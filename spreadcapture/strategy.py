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
    SELL_SIDE_ENABLED,
    MIN_INVENTORY_TO_SELL,
    ASK_OFFSET_MULTIPLIER,
)

if TYPE_CHECKING:
    from bot.orderbook import Orderbook
    from bot.market_finder import MarketPair
    from bot.position_tracker import PositionTracker
    from spreadcapture.risk_manager import SpreadCaptureRiskManager

log = logging.getLogger(__name__)


@dataclass
class QuoteSignal:
    """Result of a strategy evaluation – represents both bid and ask quotes."""
    # Bid side (passive buys)
    quote_up: float      # bid price for UP side
    quote_down: float    # bid price for DOWN side
    size_up: int         # shares to bid on UP
    size_down: int       # shares to bid on DOWN
    # Ask side (passive sells of existing inventory)
    ask_up: float = 0.0       # ask price to sell UP shares
    ask_down: float = 0.0     # ask price to sell DOWN shares
    size_ask_up: int = 0      # shares to offer on UP ask
    size_ask_down: int = 0    # shares to offer on DOWN ask
    # Metrics
    spread: float = 0.0       # captured bid spread = 1.0 − quote_up − quote_down
    mid_up: float = 0.0       # current mid of UP orderbook
    mid_down: float = 0.0     # current mid of DOWN orderbook

    @property
    def combined_cost(self) -> float:
        return self.quote_up + self.quote_down

    @property
    def edge(self) -> float:
        """Extra profit above break-even."""
        return self.spread - TARGET_SPREAD

    @property
    def has_asks(self) -> bool:
        return self.size_ask_up > 0 or self.size_ask_down > 0
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

        # ── Bid size calculation ──────────────────────────────────────────
        size_up = min(ORDER_SIZE, max(0, MAX_INVENTORY - inv_up))
        size_down = min(ORDER_SIZE, max(0, MAX_INVENTORY - inv_down))

        # ── Ask (sell) quotes – symmetric above mid ───────────────────────
        # The bid pulled the quote below mid by bid_offset; the ask goes
        # the same distance above mid (optionally scaled by ASK_OFFSET_MULTIPLIER).
        bid_offset_up = mid_up - quote_up
        bid_offset_down = mid_down - quote_down

        ask_up_raw = mid_up + bid_offset_up * ASK_OFFSET_MULTIPLIER
        ask_down_raw = mid_down + bid_offset_down * ASK_OFFSET_MULTIPLIER

        ask_up = round(min(0.99, max(mid_up + 0.01, ask_up_raw)), 2)
        ask_down = round(min(0.99, max(mid_down + 0.01, ask_down_raw)), 2)

        if SELL_SIDE_ENABLED:
            size_ask_up = min(ORDER_SIZE, inv_up) if inv_up >= MIN_INVENTORY_TO_SELL else 0
            size_ask_down = min(ORDER_SIZE, inv_down) if inv_down >= MIN_INVENTORY_TO_SELL else 0
        else:
            size_ask_up = size_ask_down = 0

        if size_up == 0 and size_down == 0 and size_ask_up == 0 and size_ask_down == 0:
            return None

        return QuoteSignal(
            quote_up=quote_up,
            quote_down=quote_down,
            size_up=size_up,
            size_down=size_down,
            ask_up=ask_up,
            ask_down=ask_down,
            size_ask_up=size_ask_up,
            size_ask_down=size_ask_down,
            spread=1.0 - quote_up - quote_down,
            mid_up=mid_up,
            mid_down=mid_down,
        )
