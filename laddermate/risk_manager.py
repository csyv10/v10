"""LadderMate risk controls.

Simpler than Dutch Book risk — the main guard is the per-market loss cap.
Exit window suppression is handled by the strategy itself.
"""

from __future__ import annotations

import logging
from typing import Optional

from laddermate.config import LOSS_CAP, MAX_SIDE_COST, STOP_BEFORE_END_SEC
from bot.market_finder import MarketPair
from laddermate.position_tracker import LadderPositionTracker

log = logging.getLogger(__name__)


class LadderRiskManager:
    def __init__(self, tracker: LadderPositionTracker) -> None:
        self._tracker = tracker
        self._halted = False
        self._halt_reason: str = ""

    def halt(self, reason: str) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            log.error("TRADING HALTED: %s", reason)

    def resume(self) -> None:
        self._halted = False
        self._halt_reason = ""

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def check_all(self, pair: Optional[MarketPair]) -> bool:
        """Return True if it is safe to place a new rung. False = skip."""
        if self._halted:
            return False

        # Loss cap: halt if realised losses exceed LOSS_CAP for this market
        pnl = self._tracker.realised_pnl
        if pnl < -LOSS_CAP:
            self.halt(f"Loss cap exceeded: PnL=${pnl:.2f} < -${LOSS_CAP:.2f}")
            return False

        if pair is None:
            return False

        # Too close to market end
        ms_left = pair.ms_until_end
        if ms_left <= STOP_BEFORE_END_SEC * 1000:
            return False

        return True

    def can_buy_side(self, side: str) -> bool:
        """Check if we can deploy more capital on this side."""
        cost = self._tracker.side_cost(side)
        return cost < MAX_SIDE_COST

    def in_exit_window(self, pair: Optional[MarketPair]) -> bool:
        """True if we are in the exit window (suppress flips)."""
        if pair is None:
            return False
        from laddermate.config import EXIT_TTC
        ms_left = pair.ms_until_end
        return ms_left <= EXIT_TTC * 1000
