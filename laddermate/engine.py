"""LadderMate bot engine — ties together market finder, orderbooks, strategy, and risk.

Mirrors the structure of bot.bot_engine but uses the LadderMate strategy
and position tracker. Reuses shared infrastructure (client, orderbook,
market_finder) from the bot/ package.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Optional

from bot.client import PolymarketClient
from bot.market_finder import MarketFinder, MarketPair
from bot.orderbook import Orderbook
from laddermate.position_tracker import LadderPositionTracker
from laddermate.risk_manager import LadderRiskManager
from laddermate.strategy import LadderMateStrategy
from laddermate.config import EVAL_INTERVAL_SEC, SELL_CHECK_INTERVAL_SEC, SETTLE_POLL_SEC, MARKET_COOLDOWN_SEC

log = logging.getLogger(__name__)

MAX_LOG_LINES = 200


class LadderMateEngine:
    """Central coordinator for LadderMate. Runs as concurrent asyncio tasks."""

    def __init__(self, underlying: str = "BTC", paper: bool = False) -> None:
        self.underlying = underlying.upper()
        self.paper = paper
        self.client = PolymarketClient(paper=paper)

        # Shared state (read by UI)
        self.tracker = LadderPositionTracker()
        self.risk = LadderRiskManager(self.tracker)
        self.strategy = LadderMateStrategy(self.tracker, self.risk)
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

    # ── Public API ────────────────────────────────────────────────────────

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
        """Reset all PnL, positions, and logs."""
        self.tracker = LadderPositionTracker()
        self.risk = LadderRiskManager(self.tracker)
        self.strategy = LadderMateStrategy(self.tracker, self.risk)
        self.log_lines.clear()
        self.log("Stats reset")

    def skip_to_next_market(self) -> None:
        self._skip_current_market = True
        self.log("Waiting for next market before trading")

    # ── Main entry ────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log("LadderMate engine starting …")
        self.status = "SCANNING"

        try:
            await asyncio.gather(
                self._market_watcher(),
                self._strategy_loop(),
                self._ob_rest_refresher(),
                self._settle_poller(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.client.close()
            self.log("LadderMate engine stopped.")
            self.status = "STOPPED"

    # ── Market watcher ────────────────────────────────────────────────────

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

            pair_changed = (
                self._current_pair is None
                or self._current_pair.up_token_id != new_pair.up_token_id
            )

            if pair_changed:
                self.log(f"Market pair → {new_pair.short_label()}")
                self._ws_stop.set()
                await asyncio.sleep(0.5)
                self._ws_stop.clear()

                if self._current_pair is not None:
                    await self._settle_previous_market(self._current_pair)
                else:
                    self.tracker.reset_for_new_market()

                self._current_pair = new_pair
                self._market_start_ts = _time.time()
                self.strategy.reset_for_new_market()
                self.ob_up = None
                self.ob_down = None

                await self.client.cancel_open_orders()

                try:
                    self.ob_up = await self.client.get_orderbook(new_pair.up_token_id)
                    self.ob_down = await self.client.get_orderbook(new_pair.down_token_id)
                    self.log("Orderbook snapshots loaded")
                except Exception as exc:
                    self.log(f"Orderbook snapshot failed: {exc}")

                if self._skip_current_market:
                    self._skip_current_market = False
                    self.log("New market detected — beginning trading")

                asyncio.create_task(self._ws_stream(new_pair))
                self.status = "TRADING"

            if self._current_pair and self._current_pair.is_expired:
                self.log("Market expired — rotating …")
                self._current_pair = None
                self.status = "SCANNING"

            await asyncio.sleep(5)

    async def _settle_previous_market(self, old_pair: MarketPair) -> None:
        """Query outcome and settle the position tracker."""
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
            # Worst-case: assume losing side
            up_s = self.tracker.up_shares
            dn_s = self.tracker.down_shares
            worst = "DOWN" if up_s >= dn_s else "UP"
            pnl = self.tracker.settle_market(worst)
            self.log(
                f"Outcome unknown for {slug[:30] or 'market'}, "
                f"worst-case ({worst} won)  PnL ${pnl:+.2f}"
            )

    async def _ws_stream(self, pair: MarketPair) -> None:
        """Stream live orderbook updates via WebSocket."""
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

    async def _ob_rest_refresher(self) -> None:
        """Poll REST orderbook every 2s as fallback alongside WS."""
        while not self._stop.is_set():
            pair = self._current_pair
            if pair is not None:
                try:
                    self.ob_up = await self.client.get_orderbook(pair.up_token_id)
                    self.ob_down = await self.client.get_orderbook(pair.down_token_id)
                except Exception:
                    pass
            await asyncio.sleep(2)

    # ── Settlement poller ───────────────────────────────────────────────

    async def _settle_poller(self) -> None:
        """Poll the on-chain CTF token balance every SETTLE_POLL_SEC to check
        if unsettled rungs have been confirmed and are available for selling.

        Uses get_conditional_balance() which queries the CLOB's
        get_balance_allowance endpoint with AssetType.CONDITIONAL. This
        returns the actual number of settled (on-chain) shares held in the
        wallet — not just matched trades. A rung becomes sellable as soon
        as the wallet balance covers all open rungs up to and including it.
        """
        while not self._stop.is_set():
            await asyncio.sleep(SETTLE_POLL_SEC)

            pair = self._current_pair
            if pair is None or self.paper:
                continue

            unsettled = self.tracker.unsettled_rungs
            if not unsettled:
                continue

            # Query on-chain balance for each side that has unsettled rungs
            loop = asyncio.get_event_loop()
            sides_to_check = {r.side for r in unsettled}
            balances: dict[str, int] = {}

            for side in sides_to_check:
                token_id = (pair.up_token_id if side == "UP"
                            else pair.down_token_id)
                try:
                    balance = await loop.run_in_executor(
                        None, self.client.get_conditional_balance, token_id,
                    )
                    balances[side] = balance
                except Exception as exc:
                    log.debug("Settle poller balance error (%s): %s", side, exc)

            if not balances:
                continue

            # Check each unsettled rung using FIFO logic: shares settle
            # in the order they were bought.  If the on-chain balance
            # fully covers a rung → mark settled.  If it only partially
            # covers it → split the rung so the settled portion can be
            # sold immediately.
            for rung in unsettled:
                on_chain = balances.get(rung.side, 0)
                if on_chain <= 0:
                    continue

                # Shares needed for all open rungs on this side bought
                # *before* this rung (FIFO: earlier buys settle first).
                prior = sum(
                    r.shares for r in self.tracker.open_rungs
                    if r.side == rung.side and r.buy_ts < rung.buy_ts
                )
                needed = prior + rung.shares  # including this rung

                if on_chain >= needed:
                    # Fully settled
                    self.tracker.mark_settled(rung)
                    self.log(
                        f"Rung settled on-chain: {rung.shares} {rung.side} "
                        f"@ ${rung.entry_price:.4f} (balance={on_chain}) "
                        f"— ready to sell"
                    )
                elif on_chain > prior:
                    # Partially settled: some shares available, rest pending
                    available = on_chain - prior
                    settled_rung = self.tracker.split_rung(rung, available)
                    self.log(
                        f"Rung partially settled: {available}/{available + rung.shares} "
                        f"{rung.side} @ ${rung.entry_price:.4f} (balance={on_chain}) "
                        f"— {available} ready to sell, {rung.shares} pending"
                    )

    # ── Strategy loop ─────────────────────────────────────────────────────

    async def _strategy_loop(self) -> None:
        """Main trading loop — runs LadderMate strategy cycles."""
        while not self._stop.is_set():
            pair = self._current_pair

            if pair is None or self.ob_up is None or self.ob_down is None:
                await asyncio.sleep(1)
                continue

            if self._skip_current_market:
                await asyncio.sleep(1)
                continue

            # Wait for market cooldown before trading (avoid opening volatility)
            elapsed = _time.time() - self._market_start_ts
            if elapsed < MARKET_COOLDOWN_SEC:
                remaining = MARKET_COOLDOWN_SEC - elapsed
                if int(remaining) == int(MARKET_COOLDOWN_SEC) - 1:
                    self.log(f"Cooldown: waiting {MARKET_COOLDOWN_SEC:.0f}s for market to settle …")
                await asyncio.sleep(1)
                continue

            if self.risk.is_halted:
                self.status = f"HALTED: {self.risk.halt_reason}"
                # Even when halted, continue selling settled rungs that have
                # reached their target — don't leave money on the table
                try:
                    sell_signals = self.strategy.evaluate_sells(
                        self.ob_up, self.ob_down, pair,
                    )
                    for sig in sell_signals:
                        await self.strategy.execute_sell(sig, self.client, self.log)

                    # Also dump orphaned rungs from a flip that are now settled
                    orphan_signals = self.strategy.evaluate_orphan_dumps(
                        self.ob_up, self.ob_down, pair,
                    )
                    for sig in orphan_signals:
                        await self.strategy.execute_orphan_dump(
                            sig, self.client, self.log,
                        )
                except Exception as exc:
                    self.log(f"Halted-sell error: {exc}")
                await asyncio.sleep(SELL_CHECK_INTERVAL_SEC)
                continue

            try:
                await self.strategy.run_cycle(
                    self.ob_up, self.ob_down, pair,
                    self.client, self.log,
                )
            except Exception as exc:
                self.log(f"Strategy error: {exc}")
                await asyncio.sleep(1)
                continue

            # Use different sleep intervals depending on phase
            from laddermate.strategy import Phase
            if self.strategy.phase == Phase.SCOUT:
                await asyncio.sleep(EVAL_INTERVAL_SEC)
            else:
                await asyncio.sleep(SELL_CHECK_INTERVAL_SEC)
