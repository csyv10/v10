"""
opportunist_strategy.py  --  PairLock v3  (Momentum Lock)

PHILOSOPHY
  A binary market pays $1 per share to the winning side.
  We follow market momentum: always position for TARGET_PROFIT ($1)
  on the currently dominant side.  When the market flips, we rebalance
  by buying the new dominant side until target profit is restored.

STRATEGY — Single "Position Engine" runs every tick:

  1. Determine which side is dominant (price > LEAD_THRESHOLD = 0.56)
  2. Calculate profit if that side wins
  3. If below TARGET_PROFIT, buy more of the dominant side
  4. On market flip, track it and reposition to new dominant
  5. Stop if MAX_FLIPS exceeded or budget exhausted

  Math per trade:
    profit_if_dom = dom_qty - total_cost
    deficit = TARGET_PROFIT - profit_if_dom
    shares_needed = deficit / (1 - dom_price)
    usd_needed = shares_needed * dom_price

  No selling — hold to resolution.
"""

import time
from collections import deque
from typing import List, Optional, Dict


class _DummyPredictor:
    current_spot_price = None
    market_open_price  = None
    def update_spot_price(self, *a, **kw): pass
    def set_market_open_price(self, *a, **kw): pass
    def reset_for_new_market(self, *a, **kw): pass
    def predict(self): return None, 0.0, ''
    def record_market_outcome(self, *a, **kw): pass


class _Position:
    __slots__ = ('qty', 'cost', 'entry', 'signal', 'entry_time')
    def __init__(self):
        self.qty = 0.0; self.cost = 0.0
        self.entry = 0.0; self.signal = ''; self.entry_time = 0.0

    @property
    def avg(self):
        return self.cost / self.qty if self.qty > 0.001 else 0.0

    def add(self, qty, price, signal=''):
        self.cost += qty * price; self.qty += qty
        self.entry = self.avg; self.signal = signal; self.entry_time = time.time()

    def remove(self, qty):
        qty = min(qty, self.qty)
        if self.qty < 0.001: return 0.0
        cost_removed = self.avg * qty
        self.qty -= qty; self.cost -= cost_removed
        if self.qty < 0.01: self.qty = self.cost = 0.0
        return cost_removed

    def clear(self):
        self.qty = self.cost = self.entry = 0.0; self.signal = ''


class OppShotStrategy:
    """PairLock v2: Dynamic pair-cost minimiser with adaptive entry."""

    STRATEGY_NAME = 'PairLock_v3'

    # ── Budget & limits ──────────────────────────────────────────────────────
    BUDGET         = 200.0   # hard max spend per market (from constructor)
    MAX_SPEND      = 15.0    # practical max total spend per market window
    MAX_RUNGS      = 30      # max buys per side
    MIN_TRADE      = 1.00    # skip trades < this (Polymarket min = $1)

    # ── Timing ───────────────────────────────────────────────────────────────
    WARMUP_SECS    = 10.0    # observe before first trade
    URGENCY_SECS   = 60.0    # last N seconds → more aggressive
    SIDE_COOLDOWN_S = 3.0    # min seconds between buys on same side
    PRICE_HISTORY_LEN = 60   # ticks of price history to track

    # ── Position engine (momentum lock) ──────────────────────────────────────
    LEAD_THRESHOLD = 0.56    # side must be > this to be "dominant"
    TARGET_PROFIT  = 1.00    # profit target on dominant side ($1)
    MAX_FLIPS      = 4       # max dominant-side changes before stopping
    WORST_FLOOR    = -10.0   # worst-case PnL hard floor

    # ── Compat (kept for UI / get_state) ─────────────────────────────────────
    PAIR_COST_TARGET   = 0.96
    PAIR_COST_HARDSTOP = 1.00
    LEAD_MIN_PNL       = -3.0
    LEAD_TARGET_PNL    = 1.0
    RUNG_USD           = 1.0

    def __init__(self, cash_ref=None, market_budget=200.0,
                 starting_balance=1000.0, exec_sim=None):
        if cash_ref is None:
            cash_ref = {'balance': starting_balance}
        self.cash_ref      = cash_ref
        self.market_budget = market_budget
        self.exec_sim      = exec_sim

        self._pos  = {'UP': _Position(), 'DOWN': _Position()}
        self._buys = {'UP': [], 'DOWN': []}
        self._rungs = {'UP': 0, 'DOWN': 0}

        # Price history for trend/dip detection (sliding window)
        self._price_history: Dict[str, deque] = {
            'UP':   deque(maxlen=self.PRICE_HISTORY_LEN),
            'DOWN': deque(maxlen=self.PRICE_HISTORY_LEN),
        }
        self._recent_high: Dict[str, float] = {'UP': 0.0, 'DOWN': 0.0}
        self._recent_low:  Dict[str, float] = {'UP': 1.0, 'DOWN': 1.0}

        # Cooldowns
        self._last_buy_time: Dict[str, float] = {'UP': 0.0, 'DOWN': 0.0}

        # Financial tracking
        self.cash_out = 0.0; self.cash_in = 0.0
        self.realised_pnl = 0.0; self.trade_count = 0
        self.trade_log = []

        # State
        self.market_status      = 'open'
        self.current_mode       = 'scout'
        self.mode_reason        = ''
        self.resolution_outcome = None
        self.final_pnl          = None
        self.final_pnl_gross    = None
        self.payout             = 0.0
        self.last_fees_paid     = 0.0

        # Current tick prices
        self._ask = {'UP': 0.50, 'DOWN': 0.50}
        self._bid = {'UP': 0.495, 'DOWN': 0.495}
        self._tick            = 0
        self._market_open_ttc = None
        self._market_start_ts = time.time()

        # Engine stats (for UI/debug)
        self._engine_stats = {'lead': 0, 'boost': 0, 'hedge': 0, 'value': 0, 'spread': 0, 'rebalance': 0}

        # Lead/hedge tracking (updated dynamically by position engine)
        self._lead_side = None          # current dominant side
        self._hedge_side = None         # opposite of dominant
        self._last_hedge_price = None   # compat

        # Momentum tracking
        self._current_dom = None        # current dominant side ('UP'/'DOWN')
        self._flip_count  = 0           # number of dominant-side flips

        # Compat stubs
        self._pair_locked   = False
        self._arb_locked    = False
        self._primary       = None
        self._flip_counter  = 0
        self._main_side     = None
        self._trend_score   = 0.0
        self._trend_side    = None
        self.trend_predictor  = _DummyPredictor()
        self._spot_prediction = None
        self._spot_confidence = 0.0
        self._spot_reason     = ''

    # ═══════════════════════════════════════════════════════════════════════
    #  PROPERTIES
    # ═══════════════════════════════════════════════════════════════════════

    @property
    def cash(self): return self.cash_ref['balance']
    @cash.setter
    def cash(self, v): self.cash_ref['balance'] = v

    @property
    def qty_up(self):    return self._pos['UP'].qty
    @property
    def qty_down(self):  return self._pos['DOWN'].qty
    @property
    def cost_up(self):   return self._pos['UP'].cost
    @property
    def cost_down(self): return self._pos['DOWN'].cost
    @property
    def avg_up(self):    return self._pos['UP'].avg
    @property
    def avg_down(self):  return self._pos['DOWN'].avg

    @property
    def total_spent(self):
        return self._pos['UP'].cost + self._pos['DOWN'].cost

    @property
    def pair_cost(self):
        """avg_up + avg_dn — guaranteed profit when < 1.0 (with equal qty)."""
        u = self._pos['UP'].avg; d = self._pos['DOWN'].avg
        if u < 0.001 or d < 0.001: return 0.0
        return u + d

    @property
    def locked_profit(self): return self.worst_case_profit

    @property
    def best_case_profit(self):
        return max(self._pnl_if('UP'), self._pnl_if('DOWN'))

    @property
    def worst_case_profit(self):
        if self.total_spent < 0.01: return 0.0
        return min(self._pnl_if('UP'), self._pnl_if('DOWN'))

    def calculate_locked_profit(self): return self.locked_profit
    def calculate_total_fees(self): return 0.0

    @property
    def qty_ratio(self):
        """min/max qty ratio.  1.0 = perfectly balanced, 0.0 = one-sided."""
        u, d = self.qty_up, self.qty_down
        if u < 0.001 or d < 0.001: return 0.0
        return min(u, d) / max(u, d)

    # ═══════════════════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════════════════

    def _available(self):
        budget_room = max(0.0, self.market_budget - self.total_spent)
        return min(max(0.0, self.cash), budget_room)

    def _pnl_if(self, outcome):
        total_cost = self._pos['UP'].cost + self._pos['DOWN'].cost
        payout     = self._pos[outcome].qty
        return payout - total_cost + self.realised_pnl

    def _cooldown_ok(self, side):
        return (time.time() - self._last_buy_time[side]) >= self.SIDE_COOLDOWN_S

    def _hypothetical_pair_cost(self, side, price, qty):
        """What would pair_cost be if we bought `qty` of `side` at `price`?"""
        other = 'DOWN' if side == 'UP' else 'UP'
        new_cost = self._pos[side].cost + qty * price
        new_qty  = self._pos[side].qty + qty
        o_cost   = self._pos[other].cost
        o_qty    = self._pos[other].qty

        if new_qty < 0.001 or o_qty < 0.001:
            return 0.0  # can't compute yet

        new_avg = new_cost / new_qty
        o_avg   = o_cost / o_qty
        return new_avg + o_avg

    def _lead_pnl_room(self):
        """How many more $ we can spend before worst-case PnL hits LEAD_MIN_PNL.
        Uses worst_case_profit (min of BOTH sides) — protects even when
        the dominant side flips mid-market.
        Returns 0.0 if already at limit."""
        if self._lead_side is None:
            return 999.0
        wc = self.worst_case_profit
        return max(0.0, wc - self.LEAD_MIN_PNL)

    def _do_buy(self, side, price, usd, reason):
        """Buy `usd` dollars worth of `side` at `price`."""
        price = max(0.01, min(0.99, price))
        usd   = min(usd, self._available())
        if usd < self.MIN_TRADE: return None
        qty = usd / price
        return self._commit(side, price, qty, usd, reason)

    def _do_buy_qty(self, side, price, qty, reason):
        """Buy exactly `qty` shares of `side` at `price`."""
        price = max(0.01, min(0.99, price))
        usd   = qty * price
        usd   = min(usd, self._available())
        if usd < self.MIN_TRADE: return None
        qty   = usd / price
        return self._commit(side, price, qty, usd, reason)

    def _commit(self, side, price, qty, usd, reason):
        self.cash -= usd; self.cash_out += usd; self.trade_count += 1
        self._pos[side].add(qty, price, reason)
        self._buys[side].append(price)
        self._rungs[side] += 1
        self._last_buy_time[side] = time.time()
        pc = self.pair_cost
        wc = self.worst_case_profit
        print(
            f'[PairLock] {reason:12s} {side}@{price:.3f}  ${usd:.2f}  '
            f'qty={qty:.1f} | avg_up={self.avg_up:.3f} avg_dn={self.avg_down:.3f} '
            f'pair_cost={pc:.3f} | worst={wc:+.2f} best={self.best_case_profit:+.2f}'
        )
        return ('BUY', side, price, qty)

    # ═══════════════════════════════════════════════════════════════════════
    #  PRICE ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════

    def _update_price_history(self, side, price):
        self._price_history[side].append(price)
        # Rolling high/low over recent history
        hist = self._price_history[side]
        if len(hist) >= 3:
            self._recent_high[side] = max(hist)
            self._recent_low[side]  = min(hist)
        else:
            self._recent_high[side] = max(self._recent_high[side], price)
            self._recent_low[side]  = min(self._recent_low[side], price)

    def _dip_pct(self, side):
        """How far has price dipped from recent high (0.0 = at high, 0.10 = 10% dip)."""
        high = self._recent_high[side]
        if high < 0.05: return 0.0
        return max(0.0, (high - self._ask[side]) / high)

    def _is_stabilising(self, side):
        """True if price seems to be stabilising after a dip (not still falling)."""
        hist = self._price_history[side]
        if len(hist) < 5: return True  # not enough data, assume OK
        recent = list(hist)[-5:]
        # Check if last 3 ticks are within 3% of each other (not free-falling)
        mx, mn = max(recent[-3:]), min(recent[-3:])
        if mx < 0.02: return True
        return (mx - mn) / mx < 0.03

    def _spread_sum(self):
        return self._ask['UP'] + self._ask['DOWN']

    # ═══════════════════════════════════════════════════════════════════════
    #  OPPORTUNITY SCORING
    # ═══════════════════════════════════════════════════════════════════════

    def _value_score(self, side):
        """
        Score 0.0–1.0 for how attractive buying `side` is right now.
        Higher = better opportunity.
        """
        price = self._ask[side]
        score = 0.0

        # 1. Absolute cheapness: lower price = higher score (max at 0.10)
        if price < self.CHEAP_THRESHOLD:
            score += 0.3 * (self.CHEAP_THRESHOLD - price) / self.CHEAP_THRESHOLD

        # 2. Dip from recent high: bigger dip = better entry
        dip = self._dip_pct(side)
        if dip >= self.DIP_PCT_TRIGGER:
            score += min(0.3, dip * 2.0)

        # 3. Pair cost improvement: does buying this side help?
        test_qty = self.RUNG_USD / max(0.01, price)
        hyp_pc = self._hypothetical_pair_cost(side, price, test_qty)
        if 0.01 < hyp_pc < self.pair_cost and self.pair_cost > 0.01:
            improvement = self.pair_cost - hyp_pc
            score += min(0.2, improvement * 5.0)

        # 4. Position imbalance: is this the underweight side?
        other = 'DOWN' if side == 'UP' else 'UP'
        my_qty = self._pos[side].qty
        ot_qty = self._pos[other].qty
        if ot_qty > 0.5 and my_qty < ot_qty * 0.8:
            score += 0.2  # underweight bonus

        # 5. Stabilisation bonus: don't catch falling knives
        if not self._is_stabilising(side):
            score *= 0.3  # heavily penalise if still falling

        return min(1.0, score)

    # ═══════════════════════════════════════════════════════════════════════
    #  POSITION ENGINE — unified entry + rebalancing (Momentum Lock)
    # ═══════════════════════════════════════════════════════════════════════

    def _engine_position(self, up, dn, ttc, urgency_mult):
        """
        Single unified engine: always target TARGET_PROFIT on the current
        dominant side.  Handles initial entry AND flip-rebalancing.

        Math:
          profit_if_dom = dom_qty - total_cost
          deficit = TARGET_PROFIT - profit_if_dom
          shares_needed = deficit / (1 - dom_price)
          usd_needed = shares_needed * dom_price
        """
        trades = []

        # ── Determine dominant side ──────────────────────────────────────
        dom_side = None
        if up >= self.LEAD_THRESHOLD and up >= dn:
            dom_side = 'UP'
        elif dn >= self.LEAD_THRESHOLD and dn > up:
            dom_side = 'DOWN'

        if dom_side is None:
            return trades   # dead zone — no clear dominant

        dom_price  = self._ask[dom_side]
        other_side = 'DOWN' if dom_side == 'UP' else 'UP'

        # ── Track flips ─────────────────────────────────────────────────
        if self._current_dom is not None and dom_side != self._current_dom:
            self._flip_count += 1
            print(
                f'[PairLock] ⚡ FLIP #{self._flip_count}: '
                f'{self._current_dom} → {dom_side}  '
                f'UP={up:.3f} DN={dn:.3f}  '
                f'spent=${self.total_spent:.2f}'
            )

        if self._flip_count > self.MAX_FLIPS:
            return trades   # too many flips — stop trading

        self._current_dom = dom_side
        self._lead_side   = dom_side
        self._hedge_side  = other_side

        # ── Calculate deficit ────────────────────────────────────────────
        dom_profit = self._pnl_if(dom_side)
        if dom_profit >= self.TARGET_PROFIT:
            return trades   # already at target ✓

        deficit = self.TARGET_PROFIT - dom_profit
        profit_per_share = 1.0 - dom_price
        if profit_per_share < 0.02:
            return trades   # price nearly $1 — no margin

        shares_needed = deficit / profit_per_share
        usd_needed    = shares_needed * dom_price

        # ── Guards ───────────────────────────────────────────────────────
        if not self._cooldown_ok(dom_side):
            return trades
        if self._available() < self.MIN_TRADE:
            return trades

        spend_room = max(0.0, self.MAX_SPEND - self.total_spent)
        if spend_room < self.MIN_TRADE:
            return trades   # spending cap reached

        # Worst-case floor guard (only after first trade)
        wc_room = 999.0
        if self.total_spent > 0.01:
            wc = self.worst_case_profit
            wc_room = max(0.0, wc - self.WORST_FLOOR)

        usd = min(usd_needed, spend_room, wc_room, self._available())
        if usd < self.MIN_TRADE:
            return trades

        # ── Label ────────────────────────────────────────────────────────
        if self.total_spent < 0.01:
            label = 'ENTRY'
        elif self._flip_count > 0 and dom_profit < -0.01:
            label = f'REBAL#{self._flip_count}'
        else:
            label = f'TARGET({deficit:+.1f})'

        t = self._do_buy(dom_side, dom_price, usd, label)
        if t:
            trades.append(t)
            self._engine_stats['lead'] += 1

        return trades

    # ═══════════════════════════════════════════════════════════════════════
    #  MAIN TICK
    # ═══════════════════════════════════════════════════════════════════════

    def check_and_trade(
        self,
        up_price, down_price, timestamp,
        time_to_close=None,
        up_bid=None, down_bid=None,
        up_orderbook=None, down_orderbook=None,
    ):
        trades = []
        if self.market_status != 'open':
            self.current_mode = 'closed'
            return trades

        # Clamp prices
        up = max(0.02, min(0.98, up_price))
        dn = max(0.02, min(0.98, down_price))
        self._ask = {'UP': up, 'DOWN': dn}
        self._bid['UP']   = up_bid   if (up_bid   and up_bid   > 0.01) else max(0.01, up - 0.005)
        self._bid['DOWN'] = down_bid if (down_bid and down_bid > 0.01) else max(0.01, dn - 0.005)

        # Update price history
        self._update_price_history('UP', up)
        self._update_price_history('DOWN', dn)

        self._tick += 1
        if self._tick == 1:
            self._market_open_ttc = time_to_close

        ttc      = time_to_close if time_to_close is not None else 99999.0
        open_ttc = self._market_open_ttc or 300.0
        elapsed  = open_ttc - ttc

        # ── Warmup ───────────────────────────────────────────────────────
        if elapsed < self.WARMUP_SECS:
            self.current_mode = 'scout'
            self.mode_reason  = f'warmup {elapsed:.0f}/{self.WARMUP_SECS:.0f}s'
            return trades

        # ── Budget / flip limits ─────────────────────────────────────────
        if self.total_spent >= self.MAX_SPEND:
            self.current_mode = 'capped'
            self.mode_reason  = f'max spend ${self.total_spent:.1f}/{self.MAX_SPEND:.0f}'
            return trades

        if self._flip_count > self.MAX_FLIPS:
            self.current_mode = 'capped'
            self.mode_reason  = f'max flips {self._flip_count}/{self.MAX_FLIPS}'
            return trades

        # ── Urgency multiplier: more aggressive near end ─────────────────
        urgency_mult = 1.0
        if ttc < self.URGENCY_SECS:
            urgency_mult = 1.0 + (1.0 - ttc / self.URGENCY_SECS)

        # ── Single position engine ───────────────────────────────────────
        trades = self._engine_position(up, dn, ttc, urgency_mult)

        # ── Mode update ─────────────────────────────────────────────────
        dom_side = self._current_dom or ('UP' if up >= dn else 'DOWN')
        dom_profit = self._pnl_if(dom_side) if self.total_spent > 0.01 else 0.0
        wp = self.worst_case_profit

        if trades:
            self.current_mode = 'positioning'
        elif self.total_spent < 0.01:
            self.current_mode = 'scout'
        elif dom_profit >= self.TARGET_PROFIT:
            self.current_mode = 'target_met'
            self._pair_locked = True
        elif self.total_spent >= self.MAX_SPEND:
            self.current_mode = 'capped'
        elif max(up, dn) < self.LEAD_THRESHOLD:
            self.current_mode = 'dead_zone'
        else:
            self.current_mode = 'tracking'

        # Mode reason
        t_count = self._engine_stats.get('lead', 0)
        self.mode_reason = (
            f'dom={self._current_dom or "?"}  '
            f'profit_if_dom={dom_profit:+.2f}  '
            f'worst={wp:+.2f}  '
            f'spent=${self.total_spent:.2f}/{self.MAX_SPEND:.0f}  '
            f'flips={self._flip_count}  '
            f'[trades:{t_count}]'
        )

        return trades

    # ═══════════════════════════════════════════════════════════════════════
    #  RECONCILIATION (live executor feedback)
    # ═══════════════════════════════════════════════════════════════════════

    def reconcile_buy(self, side: str,
                      intended_qty: float, intended_price: float,
                      actual_qty: float,   actual_price: float):
        """Correct paper position to match actual live fill."""
        intended_cost = intended_qty * intended_price
        actual_cost   = actual_qty   * actual_price
        cost_diff     = actual_cost  - intended_cost
        qty_diff      = actual_qty   - intended_qty

        pos = self._pos[side]
        pos.qty  = max(0.0, pos.qty  + qty_diff)
        pos.cost = max(0.0, pos.cost + cost_diff)
        if pos.qty < 0.001:
            pos.qty = pos.cost = 0.0

        self.cash     = self.cash     - cost_diff
        self.cash_out = self.cash_out + cost_diff

        if actual_qty < 0.001:
            self.trade_count = max(0, self.trade_count - 1)
            if self._buys[side]:
                self._buys[side].pop()
            self._rungs[side] = max(0, self._rungs[side] - 1)
            print(f'[PairLock] reconcile_buy {side}: UNFILLED — reversed')
        else:
            print(
                f'[PairLock] reconcile_buy {side}: '
                f'{intended_qty:.2f}@{intended_price:.3f} -> '
                f'{actual_qty:.2f}@{actual_price:.3f} '
                f'(dqty={qty_diff:+.2f} d${cost_diff:+.2f})'
            )

    def reconcile_sell(self, side: str,
                       paper_qty: float, paper_price: float,
                       trade_pnl: float,
                       live_qty: float, live_price: float,
                       fail_reason: str = '', min_order_size: float = 5.0):
        """This strategy never sells, but method needed for compatibility."""
        print(f'[PairLock] reconcile_sell {side}: NOOP (buy-only strategy)')

    # ═══════════════════════════════════════════════════════════════════════
    #  RESOLUTION
    # ═══════════════════════════════════════════════════════════════════════

    def resolve_market(self, outcome, resolution_price=None):
        self.market_status = 'resolved'
        self.resolution_outcome = outcome
        self.current_mode  = 'resolved'
        winner_qty  = self._pos[outcome].qty
        total_cost  = self._pos['UP'].cost + self._pos['DOWN'].cost
        gross_pnl   = winner_qty - total_cost
        self.payout = winner_qty
        self.cash += winner_qty
        self.realised_pnl = self.final_pnl = self.final_pnl_gross = gross_pnl
        self.last_fees_paid = 0.0
        print(
            f'[PairLock] RESOLVED {outcome} | pair_cost={self.pair_cost:.3f}  '
            f'UP: {self.qty_up:.1f}@{self.avg_up:.3f}  '
            f'DN: {self.qty_down:.1f}@{self.avg_down:.3f}  '
            f'PnL={gross_pnl:+.2f}  spent=${total_cost:.2f}  '
            f'engines=[V:{self._engine_stats["value"]} '
            f'S:{self._engine_stats["spread"]} '
            f'R:{self._engine_stats["rebalance"]}]'
        )
        return gross_pnl

    def close_market(self, outcome, resolution_price=None):
        return self.resolve_market(outcome, resolution_price)

    # ═══════════════════════════════════════════════════════════════════════
    #  COMPAT / STATE
    # ═══════════════════════════════════════════════════════════════════════

    def update_spot_price(self, price, timestamp=None): pass
    def set_market_open_spot(self, price): pass
    def reset_predictor_for_new_market(self):
        self._spot_prediction = None
        self._spot_confidence = 0.0
        self._spot_reason = ''

    def get_state(self):
        up  = self._pos['UP']; dn = self._pos['DOWN']
        n_u = len(self._buys['UP']); n_d = len(self._buys['DOWN'])
        net = self.cash_out - self.cash_in
        return {
            'qty_up': up.qty, 'qty_down': dn.qty,
            'cost_up': up.cost, 'cost_down': dn.cost,
            'avg_up': self.avg_up, 'avg_down': self.avg_down,
            'pair_cost': self.pair_cost,
            'locked_profit': self.locked_profit,
            'best_case_profit': self.best_case_profit,
            'qty_ratio': self.qty_ratio, 'balance_pct': 0.0,
            'is_balanced': self.qty_ratio > 0.85,
            'trade_count': self.trade_count,
            'market_status': self.market_status,
            'resolution_outcome': self.resolution_outcome,
            'final_pnl': self.final_pnl,
            'final_pnl_gross': self.final_pnl_gross,
            'fees_paid': 0.0, 'payout': self.payout,
            'max_hedge_up': 0.0, 'max_hedge_down': 0.0,
            'current_mode': self.current_mode,
            'mode_reason': self.mode_reason,
            'cash_out': self.cash_out, 'cash_in': self.cash_in,
            'arb_locked': self._pair_locked,
            'main_side': '---', 'flip_counter': 0, 'flip_threshold': 0,
            'realised_pnl': self.realised_pnl, 'net_invested': net,
            'pnl_if_up_wins': self._pnl_if('UP'),
            'pnl_if_down_wins': self._pnl_if('DOWN'),
            'up_entry': self.avg_up, 'down_entry': self.avg_down,
            'up_stop': 0.0, 'down_stop': 0.0,
            'up_signal': up.signal, 'down_signal': dn.signal,
            'obk_score_up': 0.0, 'obk_score_down': 0.0,
            'profit_goal': 0.0,
            'goal_reached': (self.pair_cost > 0.01
                             and self.pair_cost <= self.PAIR_COST_TARGET),
            'loss_limit': 0.0, 'loss_limit_hit': False,
            'n_buys_up': n_u, 'n_buys_dn': n_d,
            'n_pairs': min(n_u, n_d),
            'worst_case': self.worst_case_profit,
            'engine_stats': dict(self._engine_stats),
            'spread_sum': self._spread_sum(),
        }

    def reset_for_new_market(self):
        self._pos  = {'UP': _Position(), 'DOWN': _Position()}
        self._buys = {'UP': [], 'DOWN': []}
        self._rungs = {'UP': 0, 'DOWN': 0}
        self._price_history = {
            'UP':   deque(maxlen=self.PRICE_HISTORY_LEN),
            'DOWN': deque(maxlen=self.PRICE_HISTORY_LEN),
        }
        self._recent_high = {'UP': 0.0, 'DOWN': 0.0}
        self._recent_low  = {'UP': 1.0, 'DOWN': 1.0}
        self._last_buy_time = {'UP': 0.0, 'DOWN': 0.0}
        self.cash_out = 0.0; self.cash_in = 0.0
        self.realised_pnl = 0.0; self.trade_count = 0; self.trade_log = []
        self.market_status = 'open'; self.current_mode = 'scout'
        self.mode_reason = ''; self.resolution_outcome = None
        self.final_pnl = self.final_pnl_gross = None
        self.payout = self.last_fees_paid = 0.0
        self._tick = 0; self._market_open_ttc = None
        self._market_start_ts = time.time()
        self._engine_stats = {'lead': 0, 'boost': 0, 'hedge': 0, 'value': 0, 'spread': 0, 'rebalance': 0}
        self._lead_side = None
        self._hedge_side = None
        self._last_hedge_price = None
        self._current_dom = None
        self._flip_count  = 0
        self._pair_locked = False
        self._arb_locked  = False
        self._spot_prediction = None
        self._spot_confidence = 0.0
        self._spot_reason = ''
