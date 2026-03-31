#!/usr/bin/env python3
"""
Gaba Strategy v3 — Maker-Based Dutch Book

Post limit BUY orders BELOW ask on both sides.
When filled, avg_sum < 1.0 = locked profit.
0% maker fee + rebate.

Flow:
  1. Read orderbook → find best bid/ask for each side
  2. Post BUY limit @best_bid+1tick (top of bid queue)
  3. Wait for fill (someone sells into our bid)
  4. Repeat on whichever side needs more shares
  5. Hold everything to resolution
"""

from __future__ import annotations
import time
from typing import Optional, List, Tuple


class Side:
    __slots__ = ('qty', 'cost')
    def __init__(self):
        self.qty = 0.0
        self.cost = 0.0

    @property
    def avg(self):
        return self.cost / self.qty if self.qty > 0.01 else 0.0

    def buy(self, qty, price):
        self.cost += qty * price
        self.qty += qty

    def clear(self):
        self.qty = self.cost = 0.0


class GabaStrategy:

    # ── Tuning ────────────────────────────────────────────────────
    ORDER_SIZE       = 5       # shares per order
    BUY_INTERVAL     = 5.0     # seconds between order attempts
    STOP_BEFORE_END  = 20      # stop buying last 20s
    BID_OFFSET       = 0.01    # place bid at best_bid + this (top of queue)
    LOTTERY_PRICE    = 0.05    # below this = lottery (aggressive)
    LOTTERY_SIZE     = 15      # bigger orders in lottery mode

    def __init__(self, market_budget=1000.0, starting_balance=1000.0, exec_sim=None, **kw):
        self._pos = {'UP': Side(), 'DOWN': Side()}
        self.cash = starting_balance
        self.cash_out = 0.0
        self.cash_in = 0.0
        self.trade_count = 0
        self.realised_pnl = 0.0
        self.payout = 0.0
        self.final_pnl = 0.0
        self.final_pnl_gross = 0.0
        self.last_fees_paid = 0.0
        self.market_status = 'open'
        self.resolution_outcome = None
        self.current_mode = 'idle'
        self.mode_reason = ''
        self._last_buy_time = 0.0

        # compat
        self.trade_log = []
        self._rungs = []
        self.mirror_mode = False
        self.cash_ref = None
        self._spot_prediction = None
        self._spot_confidence = 0.0
        self._spot_reason = ''
        self.PROFIT_GOAL = 0.5
        self.LOSS_LIMIT = -10.0
        self.MAX_LOSS_PER_MARKET = 10.0

    # ── properties ────────────────────────────────────────────────
    @property
    def qty_up(self): return self._pos['UP'].qty
    @property
    def qty_down(self): return self._pos['DOWN'].qty
    @property
    def cost_up(self): return self._pos['UP'].cost
    @property
    def cost_down(self): return self._pos['DOWN'].cost
    @property
    def avg_up(self): return self._pos['UP'].avg
    @property
    def avg_down(self): return self._pos['DOWN'].avg
    @property
    def avg_sum(self):
        a, b = self.avg_up, self.avg_down
        return a + b if a > 0 and b > 0 else 0.0
    @property
    def pair_cost(self): return self.avg_sum
    @property
    def total_cost(self):
        return self._pos['UP'].cost + self._pos['DOWN'].cost
    @property
    def delta_pct(self):
        t = self.qty_up + self.qty_down
        return abs(self.qty_up - self.qty_down) / t * 100 if t > 0.01 else 0.0
    @property
    def pnl_if_up(self):
        return self._pos['UP'].qty - self.total_cost
    @property
    def pnl_if_down(self):
        return self._pos['DOWN'].qty - self.total_cost
    @property
    def worst_case(self):
        if self.total_cost < 0.01: return 0.0
        return min(self.pnl_if_up, self.pnl_if_down)
    @property
    def locked_profit(self):
        w = self.worst_case
        return w if w > 0 else 0.0
    @property
    def best_case_profit(self):
        return max(self.pnl_if_up, self.pnl_if_down) if self.total_cost > 0.01 else 0.0
    @property
    def qty_ratio(self):
        return self.qty_up / self.qty_down if self.qty_down > 0.01 else (999 if self.qty_up > 0 else 0)

    def _pnl_if(self, outcome):
        payout = self._pos[outcome].qty
        return payout - self.total_cost + self.realised_pnl

    # ── orderbook helpers ─────────────────────────────────────────
    def _best_bid(self, orderbook):
        """Extract best bid price from orderbook dict."""
        if not orderbook:
            return None
        bids = orderbook.get('bids', [])
        if not bids:
            return None
        try:
            px = bids[0]
            if isinstance(px, dict):
                return float(px.get('price', 0))
            return float(getattr(px, 'price', 0))
        except (ValueError, IndexError, TypeError):
            return None

    def _best_ask(self, orderbook):
        """Extract best ask price from orderbook dict."""
        if not orderbook:
            return None
        asks = orderbook.get('asks', [])
        if not asks:
            return None
        try:
            px = asks[0]
            if isinstance(px, dict):
                return float(px.get('price', 0))
            return float(getattr(px, 'price', 0))
        except (ValueError, IndexError, TypeError):
            return None

    def _maker_bid(self, orderbook, fallback_price):
        """Calculate our maker bid price.
        Place at best_bid (join the queue) — NOT best_bid+1tick
        which equals ask in a 1-tick spread market."""
        best_bid = self._best_bid(orderbook)
        best_ask = self._best_ask(orderbook)
        if best_bid and best_ask and best_bid > 0.02:
            spread = best_ask - best_bid
            if spread >= 0.03:
                # Wide spread — place at best_bid + 1 tick (inside the spread)
                return round(best_bid + 0.01, 2)
            else:
                # Tight spread (1-2 ticks) — place AT best_bid
                return round(best_bid, 2)
        elif best_bid and best_bid > 0.02:
            return round(best_bid, 2)
        elif best_ask and best_ask > 0.04:
            return round(best_ask - 0.02, 2)
        return round(max(0.02, fallback_price - 0.02), 2)

    # ── core logic ────────────────────────────────────────────────
    def check_and_trade(self, up_price, down_price, timestamp,
                        time_to_close=None, up_bid=None, down_bid=None,
                        up_orderbook=None, down_orderbook=None) -> List[Tuple]:
        trades = []
        if self.market_status != 'open':
            self.current_mode = 'closed'
            return trades

        up = max(0.01, min(0.99, up_price))
        dn = max(0.01, min(0.99, down_price))
        ttc = time_to_close if time_to_close is not None else 300.0
        now = time.time()

        # ── stop near close ───────────────────────────────────────
        if ttc < self.STOP_BEFORE_END:
            self.current_mode = 'holding'
            self.mode_reason = (f'HOLD {ttc:.0f}s | worst=${self.worst_case:.2f} '
                               f'best=${self.best_case_profit:.2f} sum={self.avg_sum:.4f}')
            return trades

        # ── throttle ──────────────────────────────────────────────
        if now - self._last_buy_time < self.BUY_INTERVAL:
            return trades

        # ── calculate maker bid prices ────────────────────────────
        up_bid_px = self._maker_bid(up_orderbook, up)
        dn_bid_px = self._maker_bid(down_orderbook, dn)

        # ── decide which side to buy ──────────────────────────────
        buy_side = None
        buy_price = 0.0
        buy_qty = self.ORDER_SIZE
        buy_reason = ''

        has_up = self.qty_up > 0.01
        has_dn = self.qty_down > 0.01

        # ── LOTTERY: losing side < $0.05 → aggressive buy ─────────
        if has_up and has_dn:
            if up <= self.LOTTERY_PRICE:
                buy_side, buy_price = 'UP', up_bid_px
                buy_qty = self.LOTTERY_SIZE
                buy_reason = f'LOTTERY_UP@{up:.3f}'
            elif dn <= self.LOTTERY_PRICE:
                buy_side, buy_price = 'DOWN', dn_bid_px
                buy_qty = self.LOTTERY_SIZE
                buy_reason = f'LOTTERY_DN@{dn:.3f}'

        # ── NORMAL: buy the side with worst PnL ──────────────────
        if not buy_side:
            if not has_up and not has_dn:
                # No position — buy cheaper side
                if up_bid_px <= dn_bid_px:
                    buy_side, buy_price = 'UP', up_bid_px
                else:
                    buy_side, buy_price = 'DOWN', dn_bid_px
                buy_reason = 'ENTRY'

            elif has_up and not has_dn:
                buy_side, buy_price = 'DOWN', dn_bid_px
                buy_reason = 'PAIR_DN'

            elif has_dn and not has_up:
                buy_side, buy_price = 'UP', up_bid_px
                buy_reason = 'PAIR_UP'

            else:
                # Have both — buy the weakest side
                buy_side = 'UP' if self.pnl_if_up <= self.pnl_if_down else 'DOWN'
                buy_price = up_bid_px if buy_side == 'UP' else dn_bid_px
                buy_reason = f'WEAK_{buy_side} up=${self.pnl_if_up:.2f} dn=${self.pnl_if_down:.2f}'

        # ── clamp price ───────────────────────────────────────────
        buy_price = max(0.02, min(0.98, buy_price))

        # ── execute ───────────────────────────────────────────────
        if buy_side:
            cost = buy_qty * buy_price
            self._pos[buy_side].buy(buy_qty, buy_price)
            self.cash -= cost
            self.cash_out += cost
            self.trade_count += 1
            self._last_buy_time = now
            trades.append(('BUY', buy_side, buy_price, buy_qty))

        # ── mode display ─────────────────────────────────────────
        w = self.worst_case
        if w > 0:
            self.current_mode = 'locked'
        elif has_up and has_dn:
            self.current_mode = 'arb'
        elif has_up or has_dn:
            self.current_mode = 'building'
        else:
            self.current_mode = 'scout'

        up_bid_str = f'{up_bid_px:.2f}' if up_bid_px else '?'
        dn_bid_str = f'{dn_bid_px:.2f}' if dn_bid_px else '?'
        self.mode_reason = (
            f'{buy_reason} | worst=${w:.2f} sum={self.avg_sum:.4f} '
            f'delta={self.delta_pct:.1f}% '
            f'bids: UP@{up_bid_str} DN@{dn_bid_str} | '
            f'UP={self.qty_up:.0f}@{self.avg_up:.3f} DN={self.qty_down:.0f}@{self.avg_down:.3f}'
        )
        return trades

    # ── resolution ────────────────────────────────────────────────
    def resolve_market(self, outcome):
        self.market_status = 'resolved'
        self.resolution_outcome = outcome
        up, dn = self._pos['UP'], self._pos['DOWN']

        payout = up.qty if outcome == 'UP' else dn.qty
        tc = self.total_cost
        pnl = payout - tc

        self.cash += payout
        self.cash_in += payout
        self.payout = payout
        self.realised_pnl += pnl
        self.final_pnl = self.realised_pnl
        self.final_pnl_gross = self.realised_pnl
        self.last_fees_paid = 0.0

        print(f'[Gaba] RESOLVED: {outcome} | payout=${payout:.2f} cost=${tc:.2f} '
              f'PnL=${pnl:+.2f} | sum={self.avg_sum:.4f}')

        up.clear()
        dn.clear()
        return self.final_pnl

    # ── reconcile ─────────────────────────────────────────────────
    def reconcile_buy(self, side, intended_qty, intended_price, actual_qty, actual_price):
        pos = self._pos[side]
        if actual_qty < 0.001:
            pos.qty = max(0, pos.qty - intended_qty)
            pos.cost = max(0, pos.cost - intended_qty * intended_price)
            self.cash += intended_qty * intended_price
            self.cash_out -= intended_qty * intended_price
            self.trade_count = max(0, self.trade_count - 1)
        else:
            dq = actual_qty - intended_qty
            dc = actual_qty * actual_price - intended_qty * intended_price
            pos.qty = max(0, pos.qty + dq)
            pos.cost = max(0, pos.cost + dc)
            self.cash -= dc
            self.cash_out += dc

    def reconcile_sell(self, *a, **kw):
        pass

    # ── state for UI ──────────────────────────────────────────────
    def get_state(self):
        return {
            'qty_up': self.qty_up, 'qty_down': self.qty_down,
            'cost_up': self.cost_up, 'cost_down': self.cost_down,
            'avg_up': self.avg_up, 'avg_down': self.avg_down,
            'pair_cost': self.avg_sum,
            'locked_profit': self.locked_profit,
            'best_case_profit': self.best_case_profit,
            'qty_ratio': self.qty_ratio,
            'balance_pct': self.delta_pct,
            'is_balanced': self.delta_pct <= 5.0,
            'trade_count': self.trade_count,
            'market_status': self.market_status,
            'resolution_outcome': self.resolution_outcome,
            'final_pnl': self.final_pnl, 'final_pnl_gross': self.final_pnl_gross,
            'fees_paid': 0.0, 'payout': self.payout,
            'max_hedge_up': 0.0, 'max_hedge_down': 0.0,
            'current_mode': self.current_mode, 'mode_reason': self.mode_reason,
            'cash_out': self.cash_out, 'cash_in': self.cash_in,
            'arb_locked': self.locked_profit,
            'main_side': '---', 'flip_counter': 0, 'flip_threshold': 0,
            'realised_pnl': self.realised_pnl,
            'net_invested': self.cash_out - self.cash_in,
            'pnl_if_up_wins': self._pnl_if('UP'),
            'pnl_if_down_wins': self._pnl_if('DOWN'),
            'up_entry': self.avg_up, 'down_entry': self.avg_down,
            'up_stop': 0.0, 'down_stop': 0.0,
            'up_signal': '', 'down_signal': '',
            'obk_score_up': 0.0, 'obk_score_down': 0.0,
            'profit_goal': 0.5, 'goal_reached': self.locked_profit >= 0.5,
            'loss_limit': -10.0, 'loss_limit_hit': False,
            'open_rungs_up': 0, 'open_rungs_down': 0,
            'recovery_target': 0.0, 'ladder_side': '---',
        }

    # ── stubs ─────────────────────────────────────────────────────
    def update_spot_price(self, *a, **kw): pass
    def set_market_open_spot(self, *a, **kw): pass
    def reset_predictor_for_new_market(self): pass
    def reset_for_new_market(self): pass
    def calculate_total_fees(self): return 0.0
    def calculate_locked_profit(self): return self.locked_profit
