"""Spread Capture Engine – ties together market finding, orderbooks, and the
passive quoting strategy."""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import WS_URL                         # shared Polymarket WS URL
from bot.client import PolymarketClient
from bot.market_finder import MarketFinder, MarketPair
from bot.orderbook import Orderbook
from bot.position_tracker import PositionTracker
from spreadcapture.config import REFRESH_INTERVAL_MS, ORDERBOOK_DEPTH
from spreadcapture.risk_manager import SpreadCaptureRiskManager
from spreadcapture.strategy import SpreadCaptureStrategy, QuoteSignal

log = logging.getLogger(__name__)

MAX_LOG_LINES = 200
MAX_FILLS = 50


class SpreadCaptureEngine:
    """Central coordinator for the spread-capture market-making bot."""

    def __init__(self, underlying: str = "BTC", paper: bool = True) -> None:
        self.underlying = underlying.upper()
        self.paper = paper
        self.client = PolymarketClient(paper=paper)

        self.tracker = PositionTracker()
        self.risk = SpreadCaptureRiskManager(self.tracker)
        self.strategy = SpreadCaptureStrategy(self.tracker, self.risk)
        self.market_finder = MarketFinder(self.client, self.underlying)

        self.ob_up: Optional[Orderbook] = None
        self.ob_down: Optional[Orderbook] = None

        # Public state (read by web server)
        self.status: str = "STARTING"
        self.error: Optional[str] = None
        self.current_quotes: Optional[QuoteSignal] = None
        self.log_lines: Deque[str] = deque(maxlen=MAX_LOG_LINES)
        self.recent_fills: Deque[Dict] = deque(maxlen=MAX_FILLS)
        self.total_fills: int = 0

        # Internal
        self._stop = asyncio.Event()
        self._ws_stop = asyncio.Event()
        self._current_pair: Optional[MarketPair] = None
        self._paper_orders: List[Dict] = []   # active simulated GTC orders
        self._last_quote_ms: float = 0.0
        self._fill_checks_done: int = 0       # stats

    # ── Logging ───────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_lines.append(line)
        log.info(msg)

    # ── Control ───────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()
        self._ws_stop.set()

    def pause(self) -> None:
        self.risk.halt("Paused by user")
        self.log("Trading PAUSED")

    def resume(self) -> None:
        self.risk.resume()
        self.log("Trading RESUMED")

    def reset_stats(self) -> None:
        self.tracker = PositionTracker()
        self.risk = SpreadCaptureRiskManager(self.tracker)
        self.strategy = SpreadCaptureStrategy(self.tracker, self.risk)
        self._paper_orders.clear()
        self.current_quotes = None
        self.recent_fills.clear()
        self.total_fills = 0
        self.log_lines.clear()
        self.log("Stats reset")

    # ── Main entry ────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log(f"Spread Capture Engine starting (paper={self.paper}) …")
        self.status = "SCANNING"
        try:
            await asyncio.gather(
                self._market_watcher(),
                self._strategy_loop(),
                self._ob_rest_refresher(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.client.close()
            self.log("Engine stopped.")
            self.status = "STOPPED"

    # ── Market watcher ────────────────────────────────────────────────────

    async def _market_watcher(self) -> None:
        """Poll for an active 5-min BTC market pair every 5 s."""
        while not self._stop.is_set():
            try:
                await self.market_finder._refresh()
            except Exception as exc:
                self.log(f"Market finder error: {exc}")
                await asyncio.sleep(5)
                continue

            new_pair = self.market_finder.current

            if new_pair is None:
                self.status = "SCANNING"
                await asyncio.sleep(5)
                continue

            pair_changed = (
                self._current_pair is None
                or self._current_pair.up_token_id != new_pair.up_token_id
            )

            if pair_changed:
                self.log(f"Market pair → {new_pair.short_label()}")
                # Stop existing WS; cancel open paper orders
                self._ws_stop.set()
                self._paper_orders.clear()
                self.current_quotes = None
                await asyncio.sleep(0.3)
                self._ws_stop.clear()

                self.tracker.reset_for_new_market()
                self.strategy.reset_for_new_market()
                self._current_pair = new_pair
                self._last_quote_ms = 0.0

                self.ob_up = None
                self.ob_down = None

                try:
                    self.ob_up = await self.client.get_orderbook(new_pair.up_token_id)
                    self.ob_down = await self.client.get_orderbook(new_pair.down_token_id)
                    self.log("Orderbook snapshots loaded")
                except Exception as exc:
                    self.log(f"Orderbook snapshot failed: {exc}")

                asyncio.create_task(self._ws_stream(new_pair))
                self.status = "TRADING"

            if self._current_pair and self._current_pair.is_expired:
                self.log("Market expired – scanning for next …")
                self._current_pair = None
                self.ob_up = None
                self.ob_down = None
                self.status = "SCANNING"

            await asyncio.sleep(5)

    # ── Strategy loop ─────────────────────────────────────────────────────

    async def _strategy_loop(self) -> None:
        """Main market-making loop: every REFRESH_INTERVAL_MS ms, cancel old
        orders, simulate fills, evaluate new quotes, and post."""
        while not self._stop.is_set():
            if self.ob_up is None or self.ob_down is None or self._current_pair is None:
                await asyncio.sleep(0.5)
                continue

            now_ms = _time.monotonic() * 1000
            elapsed = now_ms - self._last_quote_ms

            if elapsed < REFRESH_INTERVAL_MS:
                # While waiting, still check for paper fills continuously
                if self.paper:
                    self._simulate_paper_fills()
                sleep_ms = max(50, REFRESH_INTERVAL_MS - elapsed)
                await asyncio.sleep(sleep_ms / 1000)
                continue

            # ── Refresh cycle ────────────────────────────────────────────
            if self.paper:
                self._simulate_paper_fills()

            # Cancel previous orders
            self._paper_orders.clear()

            # Evaluate new quotes
            signal = self.strategy.evaluate(
                self.ob_up, self.ob_down, self._current_pair
            )

            if signal is not None:
                self.current_quotes = signal
                self._place_paper_orders(signal)
                ask_info = ""
                if signal.has_asks:
                    ask_info = (
                        f" | ASK UP@{signal.ask_up:.2f}×{signal.size_ask_up}"
                        f" DN@{signal.ask_down:.2f}×{signal.size_ask_down}"
                    )
                self.log(
                    f"Quoted BID UP@{signal.quote_up:.2f}×{signal.size_up} "
                    f"DN@{signal.quote_down:.2f}×{signal.size_down}{ask_info} "
                    f"spread={signal.spread:.4f}"
                )
            else:
                self.current_quotes = None
                if self.risk.is_halted:
                    self.log(f"Quoting halted: {self.risk.halt_reason}")

            self._last_quote_ms = _time.monotonic() * 1000

    # ── Paper order placement ─────────────────────────────────────────────

    def _place_paper_orders(self, signal: QuoteSignal) -> None:
        """Record passive bid AND ask orders (paper mode). Fills are simulated on tick."""
        import uuid
        # Bid (buy) orders
        if signal.size_up > 0:
            self._paper_orders.append({
                "order_id": f"paper-{uuid.uuid4().hex[:12]}",
                "direction": "BUY",
                "side": "UP",
                "price": signal.quote_up,
                "size": signal.size_up,
                "token_id": self._current_pair.up_token_id if self._current_pair else "",
            })
        if signal.size_down > 0:
            self._paper_orders.append({
                "order_id": f"paper-{uuid.uuid4().hex[:12]}",
                "direction": "BUY",
                "side": "DOWN",
                "price": signal.quote_down,
                "size": signal.size_down,
                "token_id": self._current_pair.down_token_id if self._current_pair else "",
            })
        # Ask (sell) orders – only if we hold inventory
        if signal.size_ask_up > 0:
            self._paper_orders.append({
                "order_id": f"paper-{uuid.uuid4().hex[:12]}",
                "direction": "SELL",
                "side": "UP",
                "price": signal.ask_up,
                "size": signal.size_ask_up,
                "token_id": self._current_pair.up_token_id if self._current_pair else "",
            })
        if signal.size_ask_down > 0:
            self._paper_orders.append({
                "order_id": f"paper-{uuid.uuid4().hex[:12]}",
                "direction": "SELL",
                "side": "DOWN",
                "price": signal.ask_down,
                "size": signal.size_ask_down,
                "token_id": self._current_pair.down_token_id if self._current_pair else "",
            })

    def _simulate_paper_fills(self) -> None:
        """Check if any resting paper orders have been crossed by the market.

        BUY limit at price P fills when best ask ≤ P (seller meets our bid).
        SELL limit at price P fills when best bid ≥ P (buyer meets our ask).
        """
        if not self._paper_orders:
            return
        if self.ob_up is None or self.ob_down is None:
            return

        filled_ids = []
        for order in self._paper_orders:
            ob = self.ob_up if order["side"] == "UP" else self.ob_down
            direction = order.get("direction", "BUY")

            if direction == "BUY":
                best_price = ob.best_ask
                best_size = ob.best_ask_size
                triggered = best_price is not None and best_price <= order["price"]
            else:  # SELL
                best_price = ob.best_bid
                best_size = ob.best_bid_size
                triggered = best_price is not None and best_price >= order["price"]

            if not triggered:
                continue

            fill_qty = order["size"]
            if best_size and best_size > 0:
                fill_qty = min(fill_qty, int(best_size))
            if fill_qty <= 0:
                fill_qty = order["size"]

            if direction == "BUY":
                self.tracker.record_fill(order["side"], order["price"], fill_qty)
                self.log(
                    f"[BUY FILL] {order['side']} {fill_qty}sh @ {order['price']:.2f}"
                    f"  (ask was {best_price:.2f})"
                )
            else:
                self.tracker.record_sell(order["side"], order["price"], fill_qty)
                sell_pnl = self.tracker.position.sell_realized_pnl
                self.log(
                    f"[SELL FILL] {order['side']} {fill_qty}sh @ {order['price']:.2f}"
                    f"  (bid was {best_price:.2f})"
                    f"  realized PnL: ${self.tracker.sell_realized_pnl:.4f}"
                )

            self.total_fills += 1
            fill_rec = {
                "direction": direction,
                "side": order["side"],
                "price": order["price"],
                "shares": fill_qty,
                "time": datetime.now(tz=timezone.utc).strftime("%H:%M:%S"),
            }
            self.recent_fills.appendleft(fill_rec)

            # Check loss limit
            pnl = self.tracker.total_pnl
            from spreadcapture.config import MAX_LOSS
            if pnl < -MAX_LOSS:
                self.risk.halt(f"Loss limit: PnL=${pnl:.2f}")

            filled_ids.append(order["order_id"])

        self._paper_orders = [o for o in self._paper_orders if o["order_id"] not in filled_ids]

    # ── REST orderbook refresher ──────────────────────────────────────────

    async def _ob_rest_refresher(self) -> None:
        """Periodically refresh orderbooks via REST as a fallback to WebSocket."""
        while not self._stop.is_set():
            await asyncio.sleep(5)
            pair = self._current_pair
            if pair is None or self.ob_up is None or self.ob_down is None:
                continue
            try:
                ob_up = await self.client.get_orderbook(pair.up_token_id)
                ob_down = await self.client.get_orderbook(pair.down_token_id)
                self.ob_up = ob_up
                self.ob_down = ob_down
            except Exception as exc:
                log.debug("REST refresh error: %s", exc)

    # ── WebSocket live orderbook streaming ────────────────────────────────

    async def _ws_stream(self, pair: MarketPair) -> None:
        """Connect to Polymarket CLOB WebSocket and stream orderbook updates."""
        sub_msg = json.dumps({
            "assets_ids": pair.token_ids,
            "type": "market",
        })

        while not self._ws_stop.is_set():
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=20, close_timeout=5
                ) as ws:
                    await ws.send(sub_msg)
                    while not self._ws_stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                            events = json.loads(raw)
                            if not isinstance(events, list):
                                events = [events]
                            for event in events:
                                asset_id = event.get("asset_id", "")
                                if asset_id == pair.up_token_id and self.ob_up:
                                    self.ob_up.apply_ws_update(event)
                                elif asset_id == pair.down_token_id and self.ob_down:
                                    self.ob_down.apply_ws_update(event)
                        except asyncio.TimeoutError:
                            continue
                        except ConnectionClosed:
                            break
            except Exception as exc:
                if not self._ws_stop.is_set():
                    log.debug("WS error (%s) – reconnecting in 3 s …", exc)
                    await asyncio.sleep(3)
