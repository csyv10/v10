#!/usr/bin/env python3
"""
Execution Simulator – Realistic Order Fill Simulation

Simulates real-world trade execution for Polymarket before going live:
  1. Order Book Analysis  – walks the book to check fillability
  2. Latency Simulation   – 25 ms fictive network delay
  3. Slippage Calculation  – tracks price impact from walking the book
  4. Fill Logging          – detailed trade-by-trade slippage log

Usage:
  simulator = ExecutionSimulator(latency_ms=25)
  result = simulator.simulate_fill('UP', desired_price=0.42, qty=10.0, orderbook=book)
  # result.filled, result.fill_price, result.slippage, result.partial_qty, ...
"""

import time
import math
from dataclasses import dataclass, field
from collections import deque
from typing import Optional, List, Dict
from datetime import datetime, timezone


# ── Fill result ───────────────────────────────────────────────────
@dataclass
class FillResult:
    """Result of a simulated order fill."""
    filled: bool               # Was the order (fully or partially) filled?
    desired_price: float       # Price the bot wanted
    fill_price: float          # Volume-weighted average fill price
    desired_qty: float         # Qty the bot wanted to buy
    filled_qty: float          # Qty actually filled
    partial: bool              # Was it a partial fill?
    slippage: float            # Absolute slippage (fill_price - desired_price)
    slippage_pct: float        # Slippage as percentage
    slippage_cost: float       # Extra cost due to slippage (slippage × filled_qty)
    total_cost: float          # Actual total cost of the fill
    theoretical_cost: float    # What it would have cost at desired_price
    latency_ms: float          # Simulated latency applied
    book_depth_at_best: float  # Available qty at best ask before fill
    levels_consumed: int       # How many price levels were consumed
    fill_details: List[dict]   # Per-level fill breakdown
    reason: str                # Human-readable explanation
    timestamp: str             # ISO timestamp


# ── Slippage event for logging ────────────────────────────────────
@dataclass
class SlippageEvent:
    """A single slippage event for the log."""
    timestamp: str
    side: str                  # UP or DOWN
    desired_price: float
    fill_price: float
    desired_qty: float
    filled_qty: float
    slippage: float
    slippage_pct: float
    slippage_cost: float
    levels_consumed: int
    book_depth_at_best: float
    partial: bool
    reason: str


class ExecutionSimulator:
    """
    Simulates realistic order execution against Polymarket order books.
    
    Features:
      - Walks the order book to determine actual fill price
      - Applies configurable latency (default 25ms)
      - Logs all slippage events
      - Tracks aggregate slippage stats
      - Detects unfillable orders (insufficient liquidity)
    """

    def __init__(self, latency_ms: float = 25.0, max_slippage_pct: float = 5.0):
        """
        Args:
            latency_ms: Simulated network latency in milliseconds.
            max_slippage_pct: Maximum acceptable slippage %. Orders with
                              higher slippage are rejected.
        """
        self.latency_ms = latency_ms
        self.max_slippage_pct = max_slippage_pct

        # ── Slippage tracking ──
        self.slippage_log: deque = deque(maxlen=500)  # Last 500 events
        self.total_slippage_cost: float = 0.0
        self.total_fills: int = 0
        self.total_rejections: int = 0
        self.total_partial_fills: int = 0
        self.total_filled_volume: float = 0.0
        self.total_theoretical_cost: float = 0.0
        self.total_actual_cost: float = 0.0
        self.worst_slippage_pct: float = 0.0
        self.worst_slippage_event: Optional[SlippageEvent] = None

        # ── Per-side tracking ──
        self._side_stats: Dict[str, dict] = {
            'UP': {'fills': 0, 'slippage_cost': 0.0, 'volume': 0.0, 'rejections': 0, 'partials': 0},
            'DOWN': {'fills': 0, 'slippage_cost': 0.0, 'volume': 0.0, 'rejections': 0, 'partials': 0},
        }

        # ── Latency simulation ──
        self._last_fill_time: float = 0.0

    # ══════════════════════════════════════════════════════════════
    #  CORE: Simulate a fill against the order book
    # ══════════════════════════════════════════════════════════════

    def simulate_fill(
        self,
        side: str,
        desired_price: float,
        qty: float,
        orderbook: Optional[dict],
    ) -> FillResult:
        """
        Simulate executing a BUY order of `qty` shares at `desired_price`
        against the provided order book.

        The bot always BUYS, so we walk the ASK side of the book.

        Args:
            side: 'UP' or 'DOWN'
            desired_price: The price the bot's strategy decided on
            qty: Number of shares to buy
            orderbook: Dict with 'asks' and 'bids' lists from Polymarket CLOB

        Returns:
            FillResult with all execution details
        """
        timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]

        # ── Apply latency ──
        # In real trading, there's network latency between seeing the book
        # and the order arriving at the exchange. During this time, the book
        # can change. We simulate this by slightly degrading fill quality.
        latency_applied = self.latency_ms

        # ── No order book data → reject ──
        if not orderbook or not orderbook.get('asks'):
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            return FillResult(
                filled=False,
                desired_price=desired_price,
                fill_price=0.0,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=0.0,
                slippage_pct=0.0,
                slippage_cost=0.0,
                total_cost=0.0,
                theoretical_cost=desired_price * qty,
                latency_ms=latency_applied,
                book_depth_at_best=0.0,
                levels_consumed=0,
                fill_details=[],
                reason="No asks in order book – cannot fill",
                timestamp=timestamp,
            )

        # ── Parse and sort asks (ascending by price) ──
        asks = self._parse_book_side(orderbook.get('asks', []))
        if not asks:
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            return FillResult(
                filled=False,
                desired_price=desired_price,
                fill_price=0.0,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=0.0,
                slippage_pct=0.0,
                slippage_cost=0.0,
                total_cost=0.0,
                theoretical_cost=desired_price * qty,
                latency_ms=latency_applied,
                book_depth_at_best=0.0,
                levels_consumed=0,
                fill_details=[],
                reason="Could not parse order book asks",
                timestamp=timestamp,
            )

        # Sort asks ascending (cheapest first)
        asks.sort(key=lambda x: x['price'])

        best_ask_price = asks[0]['price']
        book_depth_at_best = asks[0]['size']

        # ── Latency penalty ──
        # During 25ms latency, some of the best-ask liquidity may get taken.
        # We model this as removing a fraction of the top-of-book liquidity.
        # At 25ms, roughly 5-15% of top level may be consumed by others.
        latency_decay_factor = 1.0 - min(0.15, self.latency_ms / 200.0)
        asks[0]['size'] *= latency_decay_factor

        # ── Latency price drift ──
        # During volatile markets, prices can move during the latency window.
        # Model this as a small adverse price shift on the first level.
        # In fast markets, the best ask often moves against you by 1-3 ticks.
        if self.latency_ms > 10 and best_ask_price < 0.95:
            # Calculate implied volatility from recent slippage
            recent_slip = [e.slippage_pct for e in list(self.slippage_log)[-10:]] if self.slippage_log else []
            avg_recent_slip = sum(recent_slip) / len(recent_slip) if recent_slip else 0.0
            # If recent trades had positive slippage, market is moving fast
            if avg_recent_slip > 0.5:
                # Shift best ask up proportional to latency and volatility
                drift = best_ask_price * 0.002 * (self.latency_ms / 25.0)
                asks[0]['price'] = min(asks[0]['price'] + drift, 0.99)

        # ── Walk the book ──
        remaining_qty = qty
        total_cost = 0.0
        fill_details = []
        levels_consumed = 0

        for level in asks:
            if remaining_qty <= 0:
                break

            level_price = level['price']
            level_size = level['size']

            if level_size <= 0:
                continue

            fill_at_level = min(remaining_qty, level_size)
            cost_at_level = fill_at_level * level_price

            fill_details.append({
                'price': level_price,
                'qty': fill_at_level,
                'cost': cost_at_level,
            })

            total_cost += cost_at_level
            remaining_qty -= fill_at_level
            levels_consumed += 1

        filled_qty = qty - remaining_qty
        partial = remaining_qty > 0 and filled_qty > 0

        if filled_qty <= 0:
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            return FillResult(
                filled=False,
                desired_price=desired_price,
                fill_price=0.0,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=0.0,
                slippage_pct=0.0,
                slippage_cost=0.0,
                total_cost=0.0,
                theoretical_cost=desired_price * qty,
                latency_ms=latency_applied,
                book_depth_at_best=book_depth_at_best,
                levels_consumed=0,
                fill_details=[],
                reason=f"Insufficient liquidity – 0 shares available in book",
                timestamp=timestamp,
            )

        # ── Calculate fill metrics ──
        vwap_fill_price = total_cost / filled_qty  # Volume-weighted average price
        theoretical_cost = desired_price * filled_qty
        slippage = vwap_fill_price - desired_price
        slippage_pct = (slippage / desired_price * 100) if desired_price > 0 else 0.0
        slippage_cost = slippage * filled_qty

        # ── Max slippage check (use epsilon to avoid float rounding rejections) ──
        if slippage_pct > self.max_slippage_pct + 1e-9:
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            reason = (
                f"Slippage {slippage_pct:.2f}% exceeds max {self.max_slippage_pct:.1f}% "
                f"(want ${desired_price:.4f}, would fill @ ${vwap_fill_price:.4f})"
            )
            return FillResult(
                filled=False,
                desired_price=desired_price,
                fill_price=vwap_fill_price,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=slippage,
                slippage_pct=slippage_pct,
                slippage_cost=slippage_cost,
                total_cost=0.0,
                theoretical_cost=theoretical_cost,
                latency_ms=latency_applied,
                book_depth_at_best=book_depth_at_best,
                levels_consumed=levels_consumed,
                fill_details=fill_details,
                reason=reason,
                timestamp=timestamp,
            )

        # ── Build reason string ──
        if slippage > 0.0001:
            if partial:
                reason = (
                    f"PARTIAL FILL with slippage: {filled_qty:.1f}/{qty:.1f} shares "
                    f"@ ${vwap_fill_price:.4f} (wanted ${desired_price:.4f}, "
                    f"slip {slippage_pct:+.3f}%, cost +${slippage_cost:.4f})"
                )
            else:
                reason = (
                    f"Filled with slippage: ${vwap_fill_price:.4f} "
                    f"(wanted ${desired_price:.4f}, slip {slippage_pct:+.3f}%, "
                    f"cost +${slippage_cost:.4f}, {levels_consumed} level(s))"
                )
        elif partial:
            reason = f"PARTIAL FILL: {filled_qty:.1f}/{qty:.1f} shares @ ${vwap_fill_price:.4f}"
        else:
            reason = f"Clean fill @ ${vwap_fill_price:.4f} (no slippage)"

        # ── Update stats ──
        self.total_fills += 1
        self.total_filled_volume += filled_qty
        self.total_theoretical_cost += theoretical_cost
        self.total_actual_cost += total_cost
        self.total_slippage_cost += max(0, slippage_cost)

        if partial:
            self.total_partial_fills += 1
            self._side_stats[side]['partials'] += 1

        self._side_stats[side]['fills'] += 1
        self._side_stats[side]['slippage_cost'] += max(0, slippage_cost)
        self._side_stats[side]['volume'] += filled_qty

        # ── Log slippage event ──
        if abs(slippage) > 0.00001 or partial:
            event = SlippageEvent(
                timestamp=timestamp,
                side=side,
                desired_price=desired_price,
                fill_price=vwap_fill_price,
                desired_qty=qty,
                filled_qty=filled_qty,
                slippage=slippage,
                slippage_pct=slippage_pct,
                slippage_cost=slippage_cost,
                levels_consumed=levels_consumed,
                book_depth_at_best=book_depth_at_best,
                partial=partial,
                reason=reason,
            )
            self.slippage_log.append(event)

            if abs(slippage_pct) > abs(self.worst_slippage_pct):
                self.worst_slippage_pct = slippage_pct
                self.worst_slippage_event = event

        self._last_fill_time = time.time()

        return FillResult(
            filled=True,
            desired_price=desired_price,
            fill_price=vwap_fill_price,
            desired_qty=qty,
            filled_qty=filled_qty,
            partial=partial,
            slippage=slippage,
            slippage_pct=slippage_pct,
            slippage_cost=slippage_cost,
            total_cost=total_cost,
            theoretical_cost=theoretical_cost,
            latency_ms=latency_applied,
            book_depth_at_best=book_depth_at_best,
            levels_consumed=levels_consumed,
            fill_details=fill_details,
            reason=reason,
            timestamp=timestamp,
        )

    def simulate_buy(
        self,
        side: str,
        desired_price: float,
        qty: float,
        orderbook: Optional[dict],
        token_id: Optional[str] = None,
    ) -> FillResult:
        """Backward-compatible alias kept for callers expecting simulate_buy."""
        return self.simulate_fill(side, desired_price, qty, orderbook)

    # ══════════════════════════════════════════════════════════════
    #  CORE: Simulate a SELL (limit) against the order book
    # ══════════════════════════════════════════════════════════════

    def simulate_sell(
        self,
        side: str,
        min_price: float,
        qty: float,
        orderbook: Optional[dict],
        token_id: Optional[str] = None,
    ) -> FillResult:
        """
        Simulate executing a SELL order of `qty` shares at >= `min_price`
        against the provided order book.

        We walk the BID side of the book (highest bid first).
        Only fills at prices >= min_price (limit sell behavior).

        Args:
            side: 'UP' or 'DOWN'
            min_price: Minimum acceptable sell price (limit price)
            qty: Number of shares to sell
            orderbook: Dict with 'asks' and 'bids' lists from Polymarket CLOB

        Returns:
            FillResult with all execution details
        """
        timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]
        latency_applied = self.latency_ms

        # ── No order book data → reject ──
        if not orderbook or not orderbook.get('bids'):
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            return FillResult(
                filled=False,
                desired_price=min_price,
                fill_price=0.0,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=0.0,
                slippage_pct=0.0,
                slippage_cost=0.0,
                total_cost=0.0,
                theoretical_cost=min_price * qty,
                latency_ms=latency_applied,
                book_depth_at_best=0.0,
                levels_consumed=0,
                fill_details=[],
                reason="No bids in order book – cannot sell",
                timestamp=timestamp,
            )

        # ── Parse and sort bids (descending by price — best bid first) ──
        bids = self._parse_book_side(orderbook.get('bids', []))
        if not bids:
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            return FillResult(
                filled=False,
                desired_price=min_price,
                fill_price=0.0,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=0.0,
                slippage_pct=0.0,
                slippage_cost=0.0,
                total_cost=0.0,
                theoretical_cost=min_price * qty,
                latency_ms=latency_applied,
                book_depth_at_best=0.0,
                levels_consumed=0,
                fill_details=[],
                reason="Could not parse order book bids",
                timestamp=timestamp,
            )

        # Sort bids descending (highest bidder first)
        bids.sort(key=lambda x: x['price'], reverse=True)

        best_bid_price = bids[0]['price']
        book_depth_at_best = bids[0]['size']

        # ── Latency penalty: some top-of-book bids may get consumed ──
        latency_decay_factor = 1.0 - min(0.15, self.latency_ms / 200.0)
        bids[0]['size'] *= latency_decay_factor

        # ── Walk the book (only fill at prices >= min_price) ──
        remaining_qty = qty
        total_proceeds = 0.0
        fill_details = []
        levels_consumed = 0

        for level in bids:
            if remaining_qty <= 0:
                break

            level_price = level['price']
            level_size = level['size']

            # Limit sell: skip bids below our minimum price
            if level_price < min_price:
                break

            if level_size <= 0:
                continue

            fill_at_level = min(remaining_qty, level_size)
            proceeds_at_level = fill_at_level * level_price

            fill_details.append({
                'price': level_price,
                'qty': fill_at_level,
                'cost': proceeds_at_level,
            })

            total_proceeds += proceeds_at_level
            remaining_qty -= fill_at_level
            levels_consumed += 1

        filled_qty = qty - remaining_qty
        partial = remaining_qty > 0 and filled_qty > 0

        if filled_qty <= 0:
            self.total_rejections += 1
            self._side_stats[side]['rejections'] += 1
            return FillResult(
                filled=False,
                desired_price=min_price,
                fill_price=best_bid_price,
                desired_qty=qty,
                filled_qty=0.0,
                partial=False,
                slippage=0.0,
                slippage_pct=0.0,
                slippage_cost=0.0,
                total_cost=0.0,
                theoretical_cost=min_price * qty,
                latency_ms=latency_applied,
                book_depth_at_best=book_depth_at_best,
                levels_consumed=0,
                fill_details=[],
                reason=f"Best bid ${best_bid_price:.4f} below min sell ${min_price:.4f}",
                timestamp=timestamp,
            )

        # ── Calculate fill metrics ──
        vwap_fill_price = total_proceeds / filled_qty
        theoretical_proceeds = min_price * filled_qty
        # For sells, positive slippage = we got MORE than min_price (good)
        slippage = vwap_fill_price - min_price
        slippage_pct = (slippage / min_price * 100) if min_price > 0 else 0.0
        slippage_cost = slippage * filled_qty  # Positive = bonus proceeds

        # ── Build reason string ──
        if partial:
            reason = (
                f"PARTIAL SELL: {filled_qty:.1f}/{qty:.1f} shares "
                f"@ ${vwap_fill_price:.4f} (min ${min_price:.4f}, "
                f"{levels_consumed} level(s))"
            )
        elif abs(slippage) > 0.0001:
            reason = (
                f"Sell filled @ ${vwap_fill_price:.4f} "
                f"(min ${min_price:.4f}, slip {slippage_pct:+.3f}%, "
                f"{levels_consumed} level(s))"
            )
        else:
            reason = f"Clean sell @ ${vwap_fill_price:.4f}"

        # ── Update stats ──
        self.total_fills += 1
        self.total_filled_volume += filled_qty
        self.total_theoretical_cost += theoretical_proceeds
        self.total_actual_cost += total_proceeds

        if partial:
            self.total_partial_fills += 1
            self._side_stats[side]['partials'] += 1

        self._side_stats[side]['fills'] += 1
        self._side_stats[side]['volume'] += filled_qty

        # ── Log slippage event ──
        if abs(slippage) > 0.00001 or partial:
            event = SlippageEvent(
                timestamp=timestamp,
                side=side,
                desired_price=min_price,
                fill_price=vwap_fill_price,
                desired_qty=qty,
                filled_qty=filled_qty,
                slippage=slippage,
                slippage_pct=slippage_pct,
                slippage_cost=slippage_cost,
                levels_consumed=levels_consumed,
                book_depth_at_best=book_depth_at_best,
                partial=partial,
                reason=reason,
            )
            self.slippage_log.append(event)

        self._last_fill_time = time.time()

        return FillResult(
            filled=True,
            desired_price=min_price,
            fill_price=vwap_fill_price,
            desired_qty=qty,
            filled_qty=filled_qty,
            partial=partial,
            slippage=slippage,
            slippage_pct=slippage_pct,
            slippage_cost=slippage_cost,
            total_cost=total_proceeds,
            theoretical_cost=theoretical_proceeds,
            latency_ms=latency_applied,
            book_depth_at_best=book_depth_at_best,
            levels_consumed=levels_consumed,
            fill_details=fill_details,
            reason=reason,
            timestamp=timestamp,
        )

    # ══════════════════════════════════════════════════════════════
    #  CHECK FILLABILITY (without executing)
    # ══════════════════════════════════════════════════════════════

    def check_fillability(
        self,
        side: str,
        price: float,
        qty: float,
        orderbook: Optional[dict],
    ) -> dict:
        """
        Check whether an order CAN be filled without actually recording it.
        
        Returns dict with:
          fillable: bool
          available_qty: total qty in order book
          best_ask: best ask price
          estimated_fill_price: VWAP if walking the book
          estimated_slippage_pct: expected slippage %
          depth_levels: number of price levels with liquidity
        """
        if not orderbook or not orderbook.get('asks'):
            return {
                'fillable': False,
                'available_qty': 0.0,
                'best_ask': None,
                'estimated_fill_price': None,
                'estimated_slippage_pct': None,
                'depth_levels': 0,
                'reason': 'No order book data',
            }

        asks = self._parse_book_side(orderbook.get('asks', []))
        if not asks:
            return {
                'fillable': False,
                'available_qty': 0.0,
                'best_ask': None,
                'estimated_fill_price': None,
                'estimated_slippage_pct': None,
                'depth_levels': 0,
                'reason': 'Could not parse asks',
            }

        asks.sort(key=lambda x: x['price'])
        best_ask = asks[0]['price']
        total_available = sum(a['size'] for a in asks)
        depth_levels = len(asks)

        # Walk the book
        remaining = qty
        cost = 0.0
        for level in asks:
            if remaining <= 0:
                break
            fill = min(remaining, level['size'])
            cost += fill * level['price']
            remaining -= fill

        filled = qty - remaining
        fillable = filled >= qty * 0.95  # 95% fill = acceptable

        est_fill_price = cost / filled if filled > 0 else None
        est_slippage = ((est_fill_price - price) / price * 100) if est_fill_price and price > 0 else None

        return {
            'fillable': fillable,
            'available_qty': total_available,
            'best_ask': best_ask,
            'estimated_fill_price': est_fill_price,
            'estimated_slippage_pct': est_slippage,
            'depth_levels': depth_levels,
            'reason': 'OK' if fillable else f'Only {filled:.1f}/{qty:.1f} available',
        }

    # ══════════════════════════════════════════════════════════════
    #  AGGREGATE STATS
    # ══════════════════════════════════════════════════════════════

    def get_stats(self) -> dict:
        """Get aggregate execution statistics."""
        avg_slippage_pct = 0.0
        if self.total_fills > 0 and self.total_theoretical_cost > 0:
            avg_slippage_pct = (
                (self.total_actual_cost - self.total_theoretical_cost)
                / self.total_theoretical_cost * 100
            )

        recent_slippage = list(self.slippage_log)[-20:]  # Last 20 events

        return {
            'latency_ms': self.latency_ms,
            'total_fills': self.total_fills,
            'total_rejections': self.total_rejections,
            'total_partial_fills': self.total_partial_fills,
            'total_filled_volume': round(self.total_filled_volume, 2),
            'total_slippage_cost': round(self.total_slippage_cost, 4),
            'total_theoretical_cost': round(self.total_theoretical_cost, 4),
            'total_actual_cost': round(self.total_actual_cost, 4),
            'avg_slippage_pct': round(avg_slippage_pct, 4),
            'worst_slippage_pct': round(self.worst_slippage_pct, 4),
            'fill_rate': round(
                self.total_fills / (self.total_fills + self.total_rejections) * 100, 1
            ) if (self.total_fills + self.total_rejections) > 0 else 0.0,
            'partial_fill_rate': round(
                self.total_partial_fills / self.total_fills * 100, 1
            ) if self.total_fills > 0 else 0.0,
            'pnl_impact': round(-self.total_slippage_cost, 4),  # Negative = costs money
            'side_stats': {
                side: {
                    'fills': s['fills'],
                    'rejections': s['rejections'],
                    'partials': s['partials'],
                    'slippage_cost': round(s['slippage_cost'], 4),
                    'volume': round(s['volume'], 2),
                }
                for side, s in self._side_stats.items()
            },
            'recent_slippage': [
                {
                    'time': e.timestamp,
                    'side': e.side,
                    'desired': round(e.desired_price, 4),
                    'filled': round(e.fill_price, 4),
                    'slip_pct': round(e.slippage_pct, 3),
                    'slip_cost': round(e.slippage_cost, 4),
                    'qty': round(e.filled_qty, 1),
                    'partial': e.partial,
                    'levels': e.levels_consumed,
                    'depth': round(e.book_depth_at_best, 1),
                }
                for e in recent_slippage
            ],
        }

    def get_pnl_adjustment(self) -> float:
        """
        Returns the total PnL adjustment (negative) from slippage.
        Subtract this from paper-trade PnL to get realistic PnL.
        """
        return -self.total_slippage_cost

    # ══════════════════════════════════════════════════════════════
    #  INTERNAL HELPERS
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_book_side(levels: list) -> List[dict]:
        """Parse order book levels into [{'price': float, 'size': float}, ...]"""
        parsed = []
        for level in levels:
            try:
                price = float(level.get('price', 0))
                size = float(level.get('size', 0))
                if price > 0 and size > 0:
                    parsed.append({'price': price, 'size': size})
            except (ValueError, TypeError, AttributeError):
                continue
        return parsed

    def reset_stats(self):
        """Reset all tracking stats."""
        self.slippage_log.clear()
        self.total_slippage_cost = 0.0
        self.total_fills = 0
        self.total_rejections = 0
        self.total_partial_fills = 0
        self.total_filled_volume = 0.0
        self.total_theoretical_cost = 0.0
        self.total_actual_cost = 0.0
        self.worst_slippage_pct = 0.0
        self.worst_slippage_event = None
        for s in self._side_stats.values():
            s['fills'] = 0
            s['slippage_cost'] = 0.0
            s['volume'] = 0.0
            s['rejections'] = 0
            s['partials'] = 0
