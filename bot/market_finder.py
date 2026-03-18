"""Market discovery and lifecycle management.

Finds the active 5-minute UP/DOWN pair and signals when to rotate to the next.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import STOP_BEFORE_END_MS, MARKET_POLL_SEC

log = logging.getLogger(__name__)


@dataclass
class MarketPair:
    """A matched UP / DOWN market pair."""

    # Raw market dicts from Gamma API
    up_raw: Dict
    down_raw: Dict

    # Resolved token IDs (YES side of each market)
    up_token_id: str = ""
    down_token_id: str = ""

    # Human-readable labels
    up_condition_id: str = ""
    down_condition_id: str = ""

    end_dt: Optional[datetime] = None

    def __post_init__(self) -> None:
        self.up_token_id = _yes_token_id(self.up_raw)
        self.down_token_id = _yes_token_id(self.down_raw)
        self.up_condition_id = self.up_raw.get("conditionId", "")
        self.down_condition_id = self.down_raw.get("conditionId", "")
        # Prefer the full datetime field; endDateIso is date-only and unusable for timing
        end_str = (
            self.up_raw.get("endDate") or self.up_raw.get("endDateIso")
            or self.down_raw.get("endDate") or self.down_raw.get("endDateIso")
        )
        if end_str:
            try:
                normalized = end_str.replace("Z", "+00:00")
                # Date-only strings (e.g. "2026-03-14") have no timezone — add end-of-day UTC
                if "T" not in normalized:
                    normalized = normalized + "T23:59:00+00:00"
                self.end_dt = datetime.fromisoformat(normalized)
            except ValueError:
                pass

    @property
    def token_ids(self) -> List[str]:
        return [self.up_token_id, self.down_token_id]

    @property
    def ms_until_end(self) -> float:
        if self.end_dt is None:
            return float("inf")
        now = datetime.now(tz=timezone.utc)
        return (self.end_dt - now).total_seconds() * 1000

    @property
    def is_tradeable(self) -> bool:
        return self.ms_until_end > STOP_BEFORE_END_MS

    @property
    def is_expired(self) -> bool:
        return self.ms_until_end <= 0

    def short_label(self) -> str:
        slug = self.up_raw.get("slug", "unknown")
        return slug[:40]


def _yes_token_id(market: Dict) -> str:
    """Extract the token_id for this side from a Gamma market dict.

    find_up_down_pair injects '_token_id' directly; fall back to clobTokenIds
    or the legacy tokens array for robustness.
    """
    # Primary path: injected by find_up_down_pair
    if "_token_id" in market:
        return market["_token_id"]
    # Fallback: clobTokenIds (first = Yes/Up)
    ids = market.get("clobTokenIds", [])
    if ids:
        return ids[0]
    # Legacy: tokens array
    tokens = market.get("tokens", [])
    for t in tokens:
        if t.get("outcome", "").lower() in ("yes", "up", "1"):
            return t.get("token_id", "")
    if tokens:
        return tokens[0].get("token_id", "")
    return ""


class MarketFinder:
    """Continuously scans for the current UP/DOWN 5-min pair."""

    def __init__(self, client, underlying: str) -> None:
        self._client = client
        self._underlying = underlying.upper()
        self._current: Optional[MarketPair] = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> Optional[MarketPair]:
        return self._current

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll for the active market pair; update self._current when it changes."""
        while not stop_event.is_set():
            try:
                await self._refresh()
            except Exception as exc:
                log.warning("Market finder error: %s", exc)
            await asyncio.sleep(MARKET_POLL_SEC)

    async def _refresh(self) -> None:
        pair_dicts = await self._client.find_up_down_pair(self._underlying)
        if pair_dicts is None:
            async with self._lock:
                # Only clear if the current pair has expired; otherwise keep it
                # (API can lag between 5-min windows — don't drop a valid pair)
                if self._current is None or self._current.is_expired:
                    log.info("No active 5-min UP/DOWN pair found for %s", self._underlying)
                    self._current = None
            return

        new_pair = MarketPair(up_raw=pair_dicts["up"], down_raw=pair_dicts["down"])

        async with self._lock:
            old = self._current
            if old is None or old.up_token_id != new_pair.up_token_id:
                log.info(
                    "New market pair: %s (ends %s)",
                    new_pair.short_label(),
                    new_pair.end_dt,
                )
                self._current = new_pair
