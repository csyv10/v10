"""Spread Capture strategy: passive market making on Polymarket binary markets.

How it works
────────────
In a binary UP/DOWN market the two outcomes must pay out exactly $1.00 combined
at resolution. A passive spread capture strategy quotes both sides at a discount
to the current mid-price:

  bid_up   = mid_up   − offset
  bid_down = mid_down − offset

If both passive bids fill, our combined cost is:
  cost = bid_up + bid_down  <  1.0  →  guaranteed profit regardless of outcome

The strategy controls:
  - Quotes that satisfy 1 − bid_up − bid_down ≥ TARGET_SPREAD + EDGE_THRESHOLD
  - Inventory skew: if one side has more shares, bias quotes to rebalance
  - Hard size limits: never exceed MAX_INVENTORY per side
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from spreadcapture.config import (
    TARGET_SPREAD,
    EDGE_THRESHOLD,
    ORDER_SIZE,
    MAX_INVENTORY,
    INVENTORY_SKEW,
    MIN_PRICE,
    MAX_PRICE,
)

if TYPE_CHECKING:
    from bot.orderbook import Orderbook
    from bot.market_finder import MarketPair
    from bot.position_tracker import PositionTracker
    from spreadcapture.risk_manager import SpreadCaptureRiskManager

log = logging.getLogger(__name__)


@dataclass
class QuoteSignal:
    """Result of a strategy evaluation – represents a pair of passive bids."""
    quote_up: float      # bid price for UP side
    quote_down: float    # bid price for DOWN side
    size_up: int         # shares to bid on UP
    size_down: int       # shares to bid on DOWN
    spread: float        # captured spread = 1.0 − quote_up − quote_down
    mid_up: float        # current mid of UP orderbook
    mid_down: float      # current mid of DOWN orderbook

    @property
    def combined_cost(self) -> float:
        return self.quote_up + self.quote_down

    @property
    def edge(self) -> float:
        """Extra profit above break-even."""
        return self.spread - TARGET_SPREAD


class SpreadCaptureStrategy:
    def __init__(self, tracker: "PositionTracker", risk: "SpreadCaptureRiskManager") -> None:
        self._tracker = tracker
        self._risk = risk

    def reset_for_new_market(self) -> None:
        pass  # stateless across markets

    def evaluate(
        self,
        ob_up: "Orderbook",
        ob_down: "Orderbook",
        pair: "MarketPair",
    ) -> Optional[QuoteSignal]:
        """Compute a QuoteSignal if market conditions are suitable, else None."""

        # ── Risk gate ─────────────────────────────────────────────────────
        if not self._risk.check_all(pair):
            return None

        # ── Orderbook data ────────────────────────────────────────────────
        bid_up = ob_up.best_bid
        ask_up = ob_up.best_ask
        bid_down = ob_down.best_bid
        ask_down = ob_down.best_ask

        if None in (bid_up, ask_up, bid_down, ask_down):
            return None

        # ── Mid-price and offset ──────────────────────────────────────────
        mid_up = (bid_up + ask_up) / 2
        mid_down = (bid_down + ask_down) / 2
        sum_mids = mid_up + mid_down

        if sum_mids <= 0.0:
            return None

        # We need: quote_up + quote_down ≤ 1.0 - TARGET_SPREAD
        # Allocate the "budget" proportionally to each mid
        target_sum = 1.0 - TARGET_SPREAD
        if sum_mids > 0:
            ratio = target_sum / sum_mids
            quote_up = mid_up * ratio
            quote_down = mid_down * ratio
        else:
            return None

        # ── Inventory skew ────────────────────────────────────────────────
        # If UP-heavy: lower UP bid (harder to fill) and raise DOWN bid (easier)
        # The skew preserves the combined sum so captured spread is unchanged.
        pos = self._tracker.position
        inv_up = pos.up_shares
        inv_down = pos.down_shares

        if inv_up + inv_down > 0:
            imbalance = (inv_up - inv_down) / MAX_INVENTORY  # in [-1, +1]
            skew_adj = INVENTORY_SKEW * imbalance * TARGET_SPREAD / 2
            quote_up -= skew_adj
            quote_down += skew_adj

        # ── Clamp to price bounds and round to cent ticks ─────────────────
        lo = max(MIN_PRICE + 0.01, 0.01)
        hi = min(MAX_PRICE - 0.01, 0.99)
        quote_up = round(max(lo, min(hi, quote_up)), 2)
        quote_down = round(max(lo, min(hi, quote_down)), 2)

        # ── Edge check after clamping ─────────────────────────────────────
        if quote_up + quote_down > 1.0 - EDGE_THRESHOLD:
            log.debug(
                "Not enough edge: %.4f + %.4f = %.4f (need ≤ %.4f)",
                quote_up, quote_down, quote_up + quote_down, 1.0 - EDGE_THRESHOLD
            )
            return None

        # ── Size calculation ──────────────────────────────────────────────
        size_up = min(ORDER_SIZE, max(0, MAX_INVENTORY - inv_up))
        size_down = min(ORDER_SIZE, max(0, MAX_INVENTORY - inv_down))

        if size_up == 0 and size_down == 0:
            return None

        return QuoteSignal(
            quote_up=quote_up,
            quote_down=quote_down,
            size_up=size_up,
            size_down=size_down,
            spread=1.0 - quote_up - quote_down,
            mid_up=mid_up,
            mid_down=mid_down,
        )
