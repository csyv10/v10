"""Bot engine — ties together market finder, orderbooks, strategy, and risk."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional


from bot.client import PolymarketClient
from bot.market_finder import MarketFinder, MarketPair
from bot.orderbook import Orderbook
from bot.position_tracker import PositionTracker
from bot.risk_manager import RiskManager
from bot.strategy import DutchBookStrategy

log = logging.getLogger(__name__)

MAX_LOG_LINES = 200


class BotEngine:
    """Central coordinator. Runs as a set of concurrent asyncio tasks."""

    def __init__(self, underlying: str = "BTC", paper: bool = False) -> None:
        self.underlying = underlying.upper()
        self.paper = paper
        self.client = PolymarketClient(paper=paper)

        # Shared state (read by UI)
        self.tracker = PositionTracker()
        self.risk = RiskManager(self.tracker)
        self.strategy = DutchBookStrategy(self.tracker, self.risk)
        self.market_finder = MarketFinder(self.client, self.underlying)

        self.ob_up: Optional[Orderbook] = None
        self.ob_down: Optional[Orderbook] = None

        self.log_lines: Deque[str] = deque(maxlen=MAX_LOG_LINES)
        self.status: str = "STARTING"
        self.error: Optional[str] = None

        self._stop = asyncio.Event()
        self._ws_stop = asyncio.Event()
        self._current_pair: Optional[MarketPair] = None
        self._skip_current_market: bool = False
        self._market_start_ts: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_lines.append(line)
        log.info(msg)

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
        """Reset all PnL, positions, trade count, and logs."""
        self.tracker = PositionTracker()
        self.risk = RiskManager(self.tracker)
        self.strategy = DutchBookStrategy(self.tracker, self.risk)
        self.log_lines.clear()
        self.log("Stats reset")

    def skip_to_next_market(self) -> None:
        """Don't trade current market — start trading when next market opens."""
        self._skip_current_market = True
        self.log("Waiting for next market before trading")

    # ── Main entry ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log("Bot engine starting …")
        self.status = "SCANNING"

        try:
            await asyncio.gather(
                self._market_watcher(),
                self._strategy_loop(),
                self._ob_rest_refresher(),
                self._position_reconciler(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.client.close()
            self.log("Bot engine stopped.")
            self.status = "STOPPED"

    # ── Market watcher ────────────────────────────────────────────────────────

    async def _market_watcher(self) -> None:
        """Continuously scans for a valid market pair and resets WS on change."""
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

            # Detect pair rotation
            pair_changed = (
                self._current_pair is None
                or self._current_pair.up_token_id != new_pair.up_token_id
            )

            if pair_changed:
                self.log(f"Market pair → {new_pair.short_label()}")
                self._ws_stop.set()
                await asyncio.sleep(0.5)  # Let WS task wind down
                self._ws_stop.clear()

                if self._current_pair is not None:
                    await self._settle_previous_market(self._current_pair)
                else:
                    self.tracker.reset_for_new_market()

                self._current_pair = new_pair
                self._market_start_ts = _time.time()
                self.strategy.reset_for_new_market()
                self.ob_up = None   # keeps strategy loop blocked until init done
                self.ob_down = None

                # Cancel any open orders from the previous market
                await self.client.cancel_open_orders()

                # Sync position BEFORE releasing the strategy loop (ob_up is still None).
                # This prevents a race where strategy records fills and then
                # _sync_position_from_api double-counts those same fills.
                await self._prewarm_clob_cache(new_pair)
                await self._sync_position_from_api()

                # Now fetch REST snapshots — strategy loop unblocks after this.
                try:
                    self.ob_up = await self.client.get_orderbook(new_pair.up_token_id)
                    self.ob_down = await self.client.get_orderbook(new_pair.down_token_id)
                    self.log("Orderbook snapshots loaded")
                except Exception as exc:
                    self.log(f"Orderbook snapshot failed: {exc}")

                if self._skip_current_market:
                    self._skip_current_market = False
                    self.log("New market detected — beginning trading")

                # Launch WebSocket streamer as background task
                asyncio.create_task(self._ws_stream(new_pair))
                self.status = "TRADING"

            # Check if current pair expired
            if self._current_pair and self._current_pair.is_expired:
                self.log("Market expired — rotating …")
                self._current_pair = None
                self.status = "SCANNING"

            await asyncio.sleep(5)

    async def _prewarm_clob_cache(self, pair: "MarketPair") -> None:
        """Pre-fetch tick_size and neg_risk for both tokens so the first order is instant."""
        if self.paper:
            return
        loop = asyncio.get_event_loop()
        for token_id in pair.token_ids:
            try:
                await loop.run_in_executor(None, self.client.clob.get_tick_size, token_id)
                await loop.run_in_executor(None, self.client.clob.get_neg_risk, token_id)
            except Exception as exc:
                self.log(f"Cache pre-warm failed for {token_id[:16]}: {exc}")

    async def _settle_previous_market(self, old_pair: "MarketPair") -> None:
        """Query Gamma API for the outcome and settle the position tracker.

        Retries up to 5 times (10 s total) waiting for outcomePrices to update.
        Falls back to worst-case settlement if the outcome cannot be determined.
        """
        slug = old_pair.up_raw.get("slug", "")
        outcome = None
        if slug:
            for _ in range(5):
                outcome = await self.client.get_market_outcome(slug)
                if outcome:
                    break
                await asyncio.sleep(2)

        if outcome:
            pnl = self.tracker.settle_market(outcome)
            self.log(f"Settled {slug[:30]} → {outcome} won  PnL ${pnl:+.2f}")
        else:
            # Worst-case: assume the side we hold more of lost
            pos = self.tracker.position
            worst = "DOWN" if pos.up_shares >= pos.down_shares else "UP"
            pnl = self.tracker.settle_market(worst)
            self.log(
                f"Outcome unknown for {slug[:30] or 'market'}, "
                f"worst-case ({worst} won)  PnL ${pnl:+.2f}"
            )

    async def _ws_stream(self, pair: "MarketPair") -> None:
        """Stream live orderbook updates via WebSocket for the given pair."""
        def on_update(token_id: str, payload: dict) -> None:
            if self.ob_up and token_id == pair.up_token_id:
                self.ob_up.apply_ws_update(payload)
            elif self.ob_down and token_id == pair.down_token_id:
                self.ob_down.apply_ws_update(payload)

        await self.client.stream_orderbooks(
            pair.token_ids,
            on_update,
            self._ws_stop,
        )

    # ── REST orderbook refresher ──────────────────────────────────────────────

    async def _ob_rest_refresher(self) -> None:
        """Poll REST orderbook every 2 s as a live-data guarantee alongside WS."""
        while not self._stop.is_set():
            pair = self._current_pair
            if pair is not None:
                try:
                    self.ob_up   = await self.client.get_orderbook(pair.up_token_id)
                    self.ob_down = await self.client.get_orderbook(pair.down_token_id)
                except Exception:
                    pass
            await asyncio.sleep(2)

    async def _sync_position_from_api(self) -> None:
        """On live mode market start, initialise position from real API fills."""
        if self.paper or self._current_pair is None:
            return
        loop = asyncio.get_event_loop()
        try:
            fills = await loop.run_in_executor(
                None, self.client.get_fills_for_market,
                self._current_pair.token_ids,
            )
            for fill in fills:
                tid   = fill.get("asset_id", "")
                size  = int(float(fill.get("size", 0)))
                price = float(fill.get("price", 0))
                if size <= 0 or price <= 0:
                    continue
                if tid == self._current_pair.up_token_id:
                    self.tracker.record_fill("UP", price, size)
                elif tid == self._current_pair.down_token_id:
                    self.tracker.record_fill("DOWN", price, size)
            if fills:
                pos = self.tracker.position
                self.log(f"Position synced from API — UP={pos.up_shares} DOWN={pos.down_shares}")
        except Exception as exc:
            self.log(f"Position sync failed: {exc}")

    async def _position_reconciler(self) -> None:
        """Every 30 s, compare tracker position against authoritative API fills.

        Corrects any under-counting caused by unreliable get_order_filled()
        responses. Never reduces a position — only adds missing shares.
        """
        while not self._stop.is_set():
            await asyncio.sleep(5)
            if self.paper or self._current_pair is None or self._market_start_ts == 0:
                continue
            loop = asyncio.get_event_loop()
            try:
                fills = await loop.run_in_executor(
                    None, self.client.get_fills_for_market,
                    self._current_pair.token_ids,
                    self._market_start_ts,
                )
                up_shares = 0; up_cost = 0.0
                down_shares = 0; down_cost = 0.0
                for f in fills:
                    size  = int(float(f.get("size", 0)))
                    price = float(f.get("price", 0))
                    if size <= 0 or price <= 0:
                        continue
                    tid = f.get("asset_id", "")
                    if tid == self._current_pair.up_token_id:
                        up_shares += size; up_cost += price * size
                    elif tid == self._current_pair.down_token_id:
                        down_shares += size; down_cost += price * size

                up_d, dn_d = self.tracker.reconcile(
                    up_shares, up_cost, down_shares, down_cost
                )
                if up_d or dn_d:
                    self.log(
                        f"Position reconciled (API correction): "
                        f"+{up_d} UP  +{dn_d} DOWN  "
                        f"→ UP={self.tracker.position.up_shares} "
                        f"DOWN={self.tracker.position.down_shares}"
                    )
            except Exception as exc:
                log.debug("Position reconcile error: %s", exc)

    # ── Strategy loop ─────────────────────────────────────────────────────────

    async def _strategy_loop(self) -> None:
        """Main trading loop — evaluates signals and executes passive limit cycles."""
        while not self._stop.is_set():
            pair = self._current_pair

            if pair is None or self.ob_up is None or self.ob_down is None:
                await asyncio.sleep(1)
                continue

            if self._skip_current_market:
                await asyncio.sleep(1)
                continue

            if self.risk.is_halted:
                self.status = f"HALTED: {self.risk.halt_reason}"
                await asyncio.sleep(2)
                continue

            try:
                signal = self.strategy.evaluate(self.ob_up, self.ob_down, pair)

                if signal is not None:
                    # execute() blocks for TRADE_COOLDOWN_MS while orders are live
                    await self.strategy.execute(
                        signal, pair, self.ob_up, self.ob_down,
                        self.client, self.log,
                        lambda: self._current_pair,
                    )
                else:
                    # No trigger — check again shortly
                    await asyncio.sleep(0.5)

            except Exception as exc:
                self.log(f"Strategy error: {exc}")
                await asyncio.sleep(1)
