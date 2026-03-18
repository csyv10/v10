"""Risk controls — all trading decisions pass through here first."""

from __future__ import annotations

import logging
from typing import Optional

from config import MAX_LOSS, STOP_BEFORE_END_MS
from bot.market_finder import MarketPair
from bot.position_tracker import PositionTracker

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, tracker: PositionTracker) -> None:
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
        """Return True if it is safe to attempt a trade.  False = skip cycle."""
        if self._halted:
            return False

        # Max loss guard
        pnl = self._tracker.total_pnl
        if pnl < -MAX_LOSS:
            self.halt(f"Max loss exceeded: PnL={pnl:.2f} < -{MAX_LOSS}")
            return False

        # Market availability
        if pair is None:
            return False

        # Stop-before-end guard
        if not pair.is_tradeable:
            ms = pair.ms_until_end
            log.info("Too close to market end (%.0f ms remaining) — skipping", ms)
            return False

        return True

