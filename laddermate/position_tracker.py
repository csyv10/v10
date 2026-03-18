"""LadderMate position tracker with per-rung BUY/SELL tracking.

Unlike the Dutch Book tracker which only buys, LadderMate actively sells
positions when the spread target is reached. Each rung is tracked individually
so we know its entry price, target price, and settlement eligibility.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from laddermate.config import RUNG_SPREAD


@dataclass
class Rung:
    """A single ladder rung — one buy position awaiting its sell."""
    side: str           # "UP" or "DOWN"
    entry_price: float  # price we bought at
    shares: int         # number of shares
    cost: float         # total cost (entry_price * shares)
    buy_ts: float       # unix timestamp of purchase
    order_id: str = ""  # CLOB order ID from the buy (used for settlement polling)
    sold: bool = False  # True once the sell order fills
    settled: bool = False  # True once the buy is confirmed on-chain (shares available to sell)
    sell_price: float = 0.0   # price we sold at (0 if unsold)
    sell_ts: float = 0.0      # unix timestamp of sale

    @property
    def target_price(self) -> float:
        """Price at which we want to sell (entry + spread)."""
        return round(self.entry_price + RUNG_SPREAD, 4)

    @property
    def pnl(self) -> float:
        """Realised PnL if sold, else 0."""
        if not self.sold:
            return 0.0
        return (self.sell_price - self.entry_price) * self.shares


@dataclass
class Fill:
    """Record of a single fill event (buy or sell)."""
    side: str        # "UP" or "DOWN"
    price: float
    shares: int
    direction: str   # "BUY" or "SELL"
    ts: str = ""     # HH:MM:SS UTC


class LadderPositionTracker:
    """Thread-safe tracker for LadderMate rung positions and PnL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rungs: List[Rung] = []
        self._fills: List[Fill] = []
        self._realised_pnl: float = 0.0
        self._trade_count: int = 0
        self._markets_traded: int = 0
        self._side_cost_up: float = 0.0    # current deployed cost on UP
        self._side_cost_down: float = 0.0  # current deployed cost on DOWN

    def reset_for_new_market(self, realised: float = 0.0) -> None:
        """Clear all rungs for a new market, carrying forward realised PnL."""
        with self._lock:
            self._realised_pnl += realised
            self._markets_traded += 1
            self._rungs = []
            self._fills = []
            self._side_cost_up = 0.0
            self._side_cost_down = 0.0

    def record_buy(self, side: str, price: float, shares: int,
                   order_id: str = "", paper: bool = False) -> Rung:
        """Record a new rung purchase. Returns the created Rung.

        In paper mode, rungs are marked as settled immediately since there
        is no real CLOB settlement to wait for.
        """
        ts_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        cost = price * shares
        rung = Rung(
            side=side,
            entry_price=price,
            shares=shares,
            cost=cost,
            buy_ts=_time.time(),
            order_id=order_id,
            settled=paper,  # paper mode: immediately sellable
        )
        with self._lock:
            self._rungs.append(rung)
            self._fills.append(Fill(side=side, price=price, shares=shares,
                                    direction="BUY", ts=ts_str))
            self._trade_count += 1
            if side == "UP":
                self._side_cost_up += cost
            else:
                self._side_cost_down += cost
        return rung

    def record_sell(self, rung: Rung, sell_price: float) -> float:
        """Mark a rung as sold. Returns the PnL for this rung."""
        ts_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            rung.sold = True
            rung.sell_price = sell_price
            rung.sell_ts = _time.time()
            pnl = rung.pnl
            self._realised_pnl += pnl
            self._fills.append(Fill(side=rung.side, price=sell_price,
                                    shares=rung.shares, direction="SELL",
                                    ts=ts_str))
            self._trade_count += 1
            # Release the cost
            if rung.side == "UP":
                self._side_cost_up -= rung.cost
            else:
                self._side_cost_down -= rung.cost
        return pnl

    def record_dump(self, rung: Rung, dump_price: float) -> float:
        """Dump a rung at bid during flip recovery. Same as sell but semantically different."""
        return self.record_sell(rung, dump_price)

    def settle_market(self, winning_side: str) -> float:
        """Called when a market resolves. Settles all unsold rungs."""
        with self._lock:
            pnl = 0.0
            for rung in self._rungs:
                if rung.sold:
                    continue
                if rung.side.upper() == winning_side.upper():
                    # Winner pays $1.00 per share
                    rung_pnl = (1.0 - rung.entry_price) * rung.shares
                else:
                    # Loser pays nothing
                    rung_pnl = -rung.cost
                pnl += rung_pnl
                rung.sold = True
                rung.sell_price = 1.0 if rung.side.upper() == winning_side.upper() else 0.0
            self._realised_pnl += pnl
            return pnl

    # ── Queries ────────────────────────────────────────────────────────────

    @property
    def open_rungs(self) -> List[Rung]:
        """All rungs that haven't been sold yet."""
        with self._lock:
            return [r for r in self._rungs if not r.sold]

    @property
    def unsettled_rungs(self) -> List[Rung]:
        """Open rungs awaiting on-chain settlement confirmation."""
        with self._lock:
            return [r for r in self._rungs if not r.sold and not r.settled]

    @property
    def sellable_rungs(self) -> List[Rung]:
        """Open rungs confirmed settled on-chain and available for selling."""
        with self._lock:
            return [r for r in self._rungs if not r.sold and r.settled]

    def mark_settled(self, rung: Rung) -> None:
        """Mark a rung as settled (shares confirmed available on-chain)."""
        with self._lock:
            rung.settled = True

    def split_rung(self, rung: Rung, settled_count: int) -> Rung:
        """Split a partially settled rung into two.

        Creates a new settled rung with *settled_count* shares and shrinks
        the original rung to hold the remaining unsettled shares.
        Returns the new settled rung.
        """
        with self._lock:
            remaining = rung.shares - settled_count

            # Create a new rung for the settled portion
            settled_rung = Rung(
                side=rung.side,
                entry_price=rung.entry_price,
                shares=settled_count,
                cost=rung.entry_price * settled_count,
                buy_ts=rung.buy_ts,
                order_id=rung.order_id,
                settled=True,
            )

            # Shrink the original rung to the unsettled remainder
            rung.shares = remaining
            rung.cost = rung.entry_price * remaining

            self._rungs.append(settled_rung)
            return settled_rung

    def open_rungs_for_side(self, side: str) -> List[Rung]:
        """Open rungs for a specific side."""
        with self._lock:
            return [r for r in self._rungs if not r.sold and r.side == side]

    @property
    def side_cost_up(self) -> float:
        with self._lock:
            return self._side_cost_up

    @property
    def side_cost_down(self) -> float:
        with self._lock:
            return self._side_cost_down

    def side_cost(self, side: str) -> float:
        if side == "UP":
            return self.side_cost_up
        return self.side_cost_down

    @property
    def total_open_cost(self) -> float:
        with self._lock:
            return self._side_cost_up + self._side_cost_down

    @property
    def realised_pnl(self) -> float:
        with self._lock:
            return self._realised_pnl

    @property
    def unrealised_pnl(self) -> float:
        """Estimate unrealised PnL assuming all open rungs hit their target."""
        with self._lock:
            return sum(
                RUNG_SPREAD * r.shares
                for r in self._rungs if not r.sold
            )

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl

    @property
    def trade_count(self) -> int:
        with self._lock:
            return self._trade_count

    @property
    def markets_traded(self) -> int:
        with self._lock:
            return self._markets_traded

    @property
    def fills(self) -> List[Fill]:
        with self._lock:
            return list(self._fills)

    @property
    def up_shares(self) -> int:
        with self._lock:
            return sum(r.shares for r in self._rungs if not r.sold and r.side == "UP")

    @property
    def down_shares(self) -> int:
        with self._lock:
            return sum(r.shares for r in self._rungs if not r.sold and r.side == "DOWN")
