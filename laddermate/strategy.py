"""LadderMate ladder buy/sell strategy.

How it works
────────────
LadderMate builds positions by buying the trending side of a 5-minute binary
options market at incrementally higher prices (a "ladder"). Each rung is an
independent trade of RUNG_USD with a spread target of RUNG_SPREAD (5 cents).

Three phases:

  1. SCOUT  — Wait for a clear leading side with price ≤ MAX_ENTRY_PRICE.
              Determine which side (UP or DOWN) is leading based on best ask.

  2. LADDER — Buy the leading side at incrementally higher prices (RUNG_STEP
              apart). For each open rung, monitor the bid and sell via GTC
              when bid ≥ entry + RUNG_SPREAD. Respect SETTLE_WAIT_SEC between
              buy and sell.

  3. FLIP RECOVER — If the opposing side's price rises above FLIP_TRIGGER for
              FLIP_CONFIRM_TICKS consecutive ticks: sell all old positions at
              bid, then immediately start a new ladder on the new leading side.

Exit window: In the last EXIT_TTC seconds, flip detection is suppressed.
Positions are held and resolved naturally at market settlement.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from enum import Enum
from typing import Optional

from laddermate.config import (
    RUNG_USD, RUNG_SPREAD, RUNG_STEP,
    MAX_ENTRY_PRICE, MIN_ENTRY_PRICE,
    FLIP_TRIGGER, FLIP_CONFIRM_TICKS,
    EVAL_INTERVAL_SEC, SELL_CHECK_INTERVAL_SEC,
    MAX_SIDE_COST,
)
from bot.orderbook import Orderbook
from bot.market_finder import MarketPair
from laddermate.position_tracker import LadderPositionTracker, Rung
from laddermate.risk_manager import LadderRiskManager

log = logging.getLogger(__name__)


class Phase(Enum):
    SCOUT = "SCOUT"
    LADDER = "LADDER"
    FLIP_RECOVER = "FLIP_RECOVER"


class LadderMateStrategy:
    def __init__(self, tracker: LadderPositionTracker, risk: LadderRiskManager) -> None:
        self._tracker = tracker
        self._risk = risk

        self._phase: Phase = Phase.SCOUT
        self._leading_side: Optional[str] = None  # "UP" or "DOWN"
        self._last_rung_price: float = 0.0        # price of the most recent rung
        self._flip_tick_count: int = 0             # consecutive ticks opposing side > FLIP_TRIGGER

    def reset_for_new_market(self) -> None:
        """Called by engine when a new market pair is detected."""
        self._phase = Phase.SCOUT
        self._leading_side = None
        self._last_rung_price = 0.0
        self._flip_tick_count = 0

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def leading_side(self) -> Optional[str]:
        return self._leading_side

    # ── Phase 1: Scout ─────────────────────────────────────────────────────

    def _evaluate_scout(
        self,
        ob_up: Orderbook,
        ob_down: Orderbook,
    ) -> Optional[str]:
        """Determine the leading side. Returns "UP", "DOWN", or None.

        The leading side is the one with the higher best bid (more likely
        to resolve in its favor). We enter if its price is in our range.
        """
        bid_up = ob_up.best_bid
        bid_down = ob_down.best_bid

        if bid_up is None or bid_down is None:
            return None

        # Determine which side is leading
        if bid_up > bid_down:
            leader = "UP"
            leader_price = bid_up
        elif bid_down > bid_up:
            leader = "DOWN"
            leader_price = bid_down
        else:
            return None  # equal — no clear trend

        # Price must be in our trading range
        if leader_price > MAX_ENTRY_PRICE or leader_price < MIN_ENTRY_PRICE:
            return None

        return leader

    # ── Phase 2: Ladder ────────────────────────────────────────────────────

    def evaluate_buy(
        self,
        ob_up: Orderbook,
        ob_down: Orderbook,
        pair: MarketPair,
    ) -> Optional[dict]:
        """Check if we should place a new rung buy.

        Returns {side, price, shares, token_id} or None.
        """
        if not self._risk.check_all(pair):
            return None

        if self._leading_side is None:
            return None

        side = self._leading_side
        ob = ob_up if side == "UP" else ob_down
        token_id = pair.up_token_id if side == "UP" else pair.down_token_id

        ask = ob.best_ask
        if ask is None:
            return None

        # Determine the next rung price
        if self._last_rung_price == 0.0:
            # First rung: buy at the current ask
            buy_price = ask
        else:
            # Subsequent rungs: step up from last rung
            next_price = round(self._last_rung_price + RUNG_STEP, 4)
            # Only buy if ask is at or above our next rung level
            if ask < next_price:
                return None
            buy_price = next_price

        # Price guard
        if buy_price > MAX_ENTRY_PRICE or buy_price < MIN_ENTRY_PRICE:
            return None

        # Capital guard
        if not self._risk.can_buy_side(side):
            return None

        # Check remaining capital headroom
        remaining = MAX_SIDE_COST - self._tracker.side_cost(side)
        rung_cost = min(RUNG_USD, remaining)
        if rung_cost < 1.0:
            return None

        # Calculate shares from dollar amount
        shares = max(1, int(rung_cost / buy_price))

        return {
            "side": side,
            "price": buy_price,
            "shares": shares,
            "token_id": token_id,
        }

    def evaluate_sells(
        self,
        ob_up: Orderbook,
        ob_down: Orderbook,
        pair: MarketPair,
    ) -> list[dict]:
        """Check which open rungs can be sold at their spread target.

        Returns list of {rung, price, token_id} for rungs whose bid ≥ target.
        """
        sells = []
        for rung in self._tracker.sellable_rungs:
            ob = ob_up if rung.side == "UP" else ob_down
            token_id = pair.up_token_id if rung.side == "UP" else pair.down_token_id
            bid = ob.best_bid
            if bid is None:
                continue
            if bid >= rung.target_price:
                sells.append({
                    "rung": rung,
                    "price": bid,
                    "token_id": token_id,
                })
        return sells

    # ── Phase 3: Flip Detection ────────────────────────────────────────────

    def check_flip(
        self,
        ob_up: Orderbook,
        ob_down: Orderbook,
        pair: MarketPair,
    ) -> bool:
        """Check if the market has flipped to the opposing side.

        Returns True if flip is confirmed (opposing side > FLIP_TRIGGER for
        FLIP_CONFIRM_TICKS consecutive checks).
        """
        if self._risk.in_exit_window(pair):
            # Suppress flip detection in exit window
            self._flip_tick_count = 0
            return False

        if self._leading_side is None:
            return False

        # Check the opposing side's bid
        opposing_side = "DOWN" if self._leading_side == "UP" else "UP"
        ob_opposing = ob_up if opposing_side == "UP" else ob_down
        opposing_bid = ob_opposing.best_bid

        if opposing_bid is None:
            self._flip_tick_count = 0
            return False

        if opposing_bid >= FLIP_TRIGGER:
            self._flip_tick_count += 1
        else:
            self._flip_tick_count = 0

        return self._flip_tick_count >= FLIP_CONFIRM_TICKS

    # ── Execution ──────────────────────────────────────────────────────────

    async def execute_buy(
        self,
        signal: dict,
        client,
        log_fn,
    ) -> Optional[Rung]:
        """Place a FOK buy order for a new rung."""
        side = signal["side"]
        price = signal["price"]
        shares = signal["shares"]
        token_id = signal["token_id"]

        loop = asyncio.get_event_loop()

        log_fn(
            f"LADDER BUY {side} {shares} @ ${price:.4f} "
            f"(target ${price + RUNG_SPREAD:.4f})"
        )

        try:
            order_id, filled = await loop.run_in_executor(
                None, client.place_market_fok,
                token_id, price, shares, "BUY",
            )
        except Exception as exc:
            log_fn(f"  Buy error: {exc}")
            return None

        if filled <= 0:
            log_fn(f"  FOK cancelled — no fill at ${price:.4f}")
            return None

        rung = self._tracker.record_buy(
            side, price, filled,
            order_id=order_id,
            paper=client.paper,
        )
        self._last_rung_price = price
        log_fn(f"  Filled {filled} {side} @ ${price:.4f} — rung #{len(self._tracker.open_rungs)}")
        return rung

    async def execute_sell(
        self,
        sell_signal: dict,
        client,
        log_fn,
    ) -> float:
        """Place a GTC sell order for a rung at its target price."""
        rung: Rung = sell_signal["rung"]
        price = sell_signal["price"]
        token_id = sell_signal["token_id"]

        loop = asyncio.get_event_loop()

        log_fn(
            f"LADDER SELL {rung.side} {rung.shares} @ ${price:.4f} "
            f"(entry ${rung.entry_price:.4f}, spread ${price - rung.entry_price:.4f})"
        )

        try:
            _, filled = await loop.run_in_executor(
                None, client.place_market_fok,
                token_id, price, rung.shares, "SELL",
            )
        except Exception as exc:
            log_fn(f"  Sell error: {exc}")
            return 0.0

        if filled <= 0:
            log_fn(f"  Sell FOK not filled — will retry")
            return 0.0

        pnl = self._tracker.record_sell(rung, price)
        log_fn(f"  Sold {filled} {rung.side} — PnL ${pnl:+.4f}")
        return pnl

    async def execute_flip(
        self,
        pair: MarketPair,
        ob_up: Orderbook,
        ob_down: Orderbook,
        client,
        log_fn,
    ) -> None:
        """Dump all positions on the old side and start a new ladder on the new side."""
        old_side = self._leading_side
        new_side = "DOWN" if old_side == "UP" else "UP"

        log_fn(f"FLIP DETECTED: {old_side} → {new_side}")

        # Dump all open rungs on the old side at current bid
        loop = asyncio.get_event_loop()
        open_rungs = self._tracker.open_rungs_for_side(old_side)

        for rung in open_rungs:
            ob = ob_up if rung.side == "UP" else ob_down
            token_id = pair.up_token_id if rung.side == "UP" else pair.down_token_id
            bid = ob.best_bid

            if bid is None or bid <= 0:
                log_fn(f"  No bid for {rung.side} — cannot dump rung @ ${rung.entry_price:.4f}")
                continue

            log_fn(f"  Dumping {rung.shares} {rung.side} @ ${bid:.4f} (entry ${rung.entry_price:.4f})")

            if not rung.settled:
                log_fn(f"  Rung not yet settled on-chain — holding")
                continue

            try:
                _, filled = await loop.run_in_executor(
                    None, client.place_market_fok,
                    token_id, bid, rung.shares, "SELL",
                )
                if filled > 0:
                    pnl = self._tracker.record_dump(rung, bid)
                    log_fn(f"  Dumped {filled} — PnL ${pnl:+.4f}")
                else:
                    log_fn(f"  Dump FOK not filled at ${bid:.4f}")
            except Exception as exc:
                log_fn(f"  Dump error: {exc}")

        # Switch to new side
        self._leading_side = new_side
        self._last_rung_price = 0.0
        self._flip_tick_count = 0
        self._phase = Phase.LADDER
        log_fn(f"  Ladder restarted on {new_side}")

    # ── Orphan dump (post-flip cleanup) ──────────────────────────────────

    def evaluate_orphan_dumps(
        self,
        ob_up: Orderbook,
        ob_down: Orderbook,
        pair: MarketPair,
    ) -> list[dict]:
        """Find settled open rungs on the opposite side from the current
        leading side. These are "orphans" left behind after a flip because
        they weren't settled on-chain at flip time. Once settled, they
        should be dumped at current bid rather than held until expiry.

        Returns list of {rung, price, token_id} — same shape as sell signals.
        """
        if self._leading_side is None:
            return []

        orphan_side = "DOWN" if self._leading_side == "UP" else "UP"
        ob = ob_up if orphan_side == "UP" else ob_down
        token_id = pair.up_token_id if orphan_side == "UP" else pair.down_token_id
        bid = ob.best_bid

        if bid is None or bid <= 0:
            return []

        dumps = []
        for rung in self._tracker.sellable_rungs:
            if rung.side == orphan_side:
                dumps.append({
                    "rung": rung,
                    "price": bid,
                    "token_id": token_id,
                })
        return dumps

    async def execute_orphan_dump(
        self,
        dump_signal: dict,
        client,
        log_fn,
    ) -> float:
        """Dump an orphaned rung at bid. Same as sell but logged differently."""
        rung: Rung = dump_signal["rung"]
        price = dump_signal["price"]
        token_id = dump_signal["token_id"]

        loop = asyncio.get_event_loop()

        log_fn(
            f"ORPHAN DUMP {rung.side} {rung.shares} @ ${price:.4f} "
            f"(entry ${rung.entry_price:.4f}, loss ${price - rung.entry_price:+.4f})"
        )

        try:
            _, filled = await loop.run_in_executor(
                None, client.place_market_fok,
                token_id, price, rung.shares, "SELL",
            )
        except Exception as exc:
            log_fn(f"  Orphan dump error: {exc}")
            return 0.0

        if filled <= 0:
            log_fn(f"  Orphan dump FOK not filled at ${price:.4f}")
            return 0.0

        pnl = self._tracker.record_dump(rung, price)
        log_fn(f"  Dumped {filled} {rung.side} — PnL ${pnl:+.4f}")
        return pnl

    # ── Main loop integration ──────────────────────────────────────────────

    async def run_cycle(
        self,
        ob_up: Orderbook,
        ob_down: Orderbook,
        pair: MarketPair,
        client,
        log_fn,
    ) -> None:
        """Run one strategy cycle. Called repeatedly by the engine."""

        # ── Scout phase ──────────────────────────────────────────────────
        if self._phase == Phase.SCOUT:
            leader = self._evaluate_scout(ob_up, ob_down)
            if leader is not None:
                self._leading_side = leader
                self._phase = Phase.LADDER
                log_fn(f"Scout → Ladder: leading side = {leader}")
            return

        # ── Ladder / Flip Recover phase ──────────────────────────────────

        # Check for sells first (always)
        sell_signals = self.evaluate_sells(ob_up, ob_down, pair)
        for sig in sell_signals:
            await self.execute_sell(sig, client, log_fn)

        # Dump any orphaned rungs from a previous flip that are now settled
        orphan_signals = self.evaluate_orphan_dumps(ob_up, ob_down, pair)
        for sig in orphan_signals:
            await self.execute_orphan_dump(sig, client, log_fn)

        # Check for flip (only if not in exit window)
        if self.check_flip(ob_up, ob_down, pair):
            self._phase = Phase.FLIP_RECOVER
            await self.execute_flip(pair, ob_up, ob_down, client, log_fn)
            return

        # Check for new rung buy
        buy_signal = self.evaluate_buy(ob_up, ob_down, pair)
        if buy_signal is not None:
            await self.execute_buy(buy_signal, client, log_fn)
