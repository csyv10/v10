"""Risk manager for the Spread Capture bot."""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from spreadcapture.config import MAX_LOSS, STOP_BEFORE_END_MS

if TYPE_CHECKING:
    from bot.market_finder import MarketPair
    from bot.position_tracker import PositionTracker

log = logging.getLogger(__name__)


class SpreadCaptureRiskManager:
    def __init__(self, tracker: "PositionTracker") -> None:
        self._tracker = tracker
        self._halted = False
        self._halt_reason: str = ""
        self._max_loss = MAX_LOSS

    def halt(self, reason: str) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            log.error("SPREAD BOT HALTED: %s", reason)

    def resume(self) -> None:
        self._halted = False
        self._halt_reason = ""

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def check_all(self, pair: Optional["MarketPair"]) -> bool:
        """Return True if it is safe to quote. False = skip this cycle."""
        if self._halted:
            return False

        pnl = self._tracker.total_pnl
        if pnl < -self._max_loss:
            self.halt(f"Loss limit hit: PnL={pnl:.2f} < -${self._max_loss}")
            return False

        if pair is None:
            return False

        if not pair.is_tradeable:
            ms = pair.ms_until_end
            log.info("Too close to market end (%.0f ms) – skipping quote", ms)
            return False

        return True
