"""Track open positions and cumulative PnL across market rotations."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Fill:
    side: str       # "UP" or "DOWN"
    price: float
    shares: int
    direction: str  # "BUY" or "SELL"
    ts: str = ""    # HH:MM:SS UTC


@dataclass
class MarketPosition:
    """Tracks fills within a single market pair lifetime."""
    up_shares: int = 0
    down_shares: int = 0
    up_cost: float = 0.0
    down_cost: float = 0.0
    fills: List[Fill] = field(default_factory=list)

    def record_fill(self, side: str, price: float, shares: int) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        self.fills.append(Fill(side=side, price=price, shares=shares, direction="BUY", ts=ts))
        if side == "UP":
            self.up_shares += shares
            self.up_cost += price * shares
        else:
            self.down_shares += shares
            self.down_cost += price * shares

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost

    @property
    def share_delta_pct(self) -> float:
        """Absolute percentage difference between UP and DOWN share counts."""
        total = self.up_shares + self.down_shares
        if total == 0:
            return 0.0
        return abs(self.up_shares - self.down_shares) / total

    @property
    def avg_up_price(self) -> Optional[float]:
        if self.up_shares == 0:
            return None
        return self.up_cost / self.up_shares

    @property
    def avg_down_price(self) -> Optional[float]:
        if self.down_shares == 0:
            return None
        return self.down_cost / self.down_shares

    @property
    def avg_sum(self) -> Optional[float]:
        u, d = self.avg_up_price, self.avg_down_price
        if u is None or d is None:
            return None
        return u + d

    def unrealised_pnl(self) -> float:
        """
        If positions were balanced and one side wins, we receive $1/share.
        Estimate = min(up_shares, down_shares) * 1.0 - total_cost_of_matched_pairs.
        """
        matched = min(self.up_shares, self.down_shares)
        if matched == 0:
            return 0.0
        matched_up_cost = self.avg_up_price * matched if self.avg_up_price else 0.0
        matched_down_cost = self.avg_down_price * matched if self.avg_down_price else 0.0
        return matched * 1.0 - (matched_up_cost + matched_down_cost)


class PositionTracker:
    """Thread-safe tracker for positions and cumulative PnL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = MarketPosition()
        self._realised_pnl: float = 0.0
        self._trade_count: int = 0
        self._peak_cost: float = 0.0  # all-time high total spend across markets
        self._markets_traded: int = 0

    def reset_for_new_market(self, realised: float = 0.0) -> None:
        with self._lock:
            # Carry forward realised PnL from previous market settlement
            self._realised_pnl += realised
            self._markets_traded += 1
            self._current = MarketPosition()

    def record_fill(self, side: str, price: float, shares: int) -> None:
        with self._lock:
            self._current.record_fill(side, price, shares)
            self._trade_count += 1
            if self._current.total_cost > self._peak_cost:
                self._peak_cost = self._current.total_cost

    def reconcile(self, up_shares: int, up_cost: float,
                  down_shares: int, down_cost: float) -> tuple[int, int]:
        """Overwrite current position with API-authoritative totals.

        Returns (up_delta, down_delta) — positive means shares were added.
        Only updates if the API reports MORE shares than we currently track
        (never reduces — a fill already recorded is a fill that happened).
        """
        with self._lock:
            up_delta   = max(0, up_shares   - self._current.up_shares)
            down_delta = max(0, down_shares - self._current.down_shares)
            if up_delta > 0:
                avg_up = up_cost / up_shares if up_shares else 0.0
                self._current.up_shares += up_delta
                self._current.up_cost   += avg_up * up_delta
                self._trade_count       += 1
            if down_delta > 0:
                avg_down = down_cost / down_shares if down_shares else 0.0
                self._current.down_shares += down_delta
                self._current.down_cost   += avg_down * down_delta
                self._trade_count         += 1
            if self._current.total_cost > self._peak_cost:
                self._peak_cost = self._current.total_cost
            return up_delta, down_delta

    def settle_market(self, winning_side: str) -> float:
        """Called when a market resolves. Returns the settled PnL for this market."""
        with self._lock:
            pos = self._current
            if winning_side.upper() == "UP":
                payout = pos.up_shares * 1.0
            else:
                payout = pos.down_shares * 1.0
            pnl = payout - pos.total_cost
            self._realised_pnl += pnl
            self._markets_traded += 1
            self._current = MarketPosition()
            return pnl

    # ── Read-only snapshots ──────────────────────────────────────────────────

    @property
    def position(self) -> MarketPosition:
        with self._lock:
            return self._current

    @property
    def unrealised_pnl(self) -> float:
        with self._lock:
            return self._current.unrealised_pnl()

    @property
    def realised_pnl(self) -> float:
        with self._lock:
            return self._realised_pnl

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl

    @property
    def trade_count(self) -> int:
        with self._lock:
            return self._trade_count

    @property
    def peak_cost(self) -> float:
        with self._lock:
            return self._peak_cost

    @property
    def markets_traded(self) -> int:
        with self._lock:
            return self._markets_traded
