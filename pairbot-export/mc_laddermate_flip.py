"""
mc_laddermate_flip.py
════════════════════════════════════════════════════════════════════════════
A/B test: baseline flip-logic vs. aggressive flip-repositioning (10 000 markets)

ROOT CAUSE of current poor flip recovery
─────────────────────────────────────────
Current formula:  sell_tgt = new_price + (open_cost + RECOVERY_TARGET) / rec_qty

With 5 rungs @ avg 0.45 => open_cost ≈ $10
  rec_qty  = $3 / 0.52   = 5.77 shares
  sell_tgt = 0.52 + 12 / 5.77 = 2.60  →  clamped to 0.92

The bot is forced to gamble on hitting 0.92 to "cover" old losses.
It almost never does — so recovery rarely fires, and the old side resolves at $0.

THREE FIXES IN "FLIP_AGGRESSIVE" VARIANT
─────────────────────────────────────────
  F1  Sunk cost principle
      Old rung losses are already gone. Set recovery sell target as a
      fixed premium above buy price (FLIP_SPREAD = 0.08), independent
      of what was spent on the old side.
      sell_tgt = new_price + FLIP_SPREAD
      This is always reachable and turns flip trades into real quick-profit plays.

  F2  Cascade flip
      On confirmed flip: immediately buy CASCADE_RUNGS × FLIP_RUNG_USD worth
      of rungs on the new side spread RUNG_STEP apart.
      Gives much larger exposure on the confirmed winner.

  F3  Faster detection
      FLIP_TRIGGER: 0.50 → 0.44   (require larger lead before flipping)
      FLIP_CONFIRM:    3 → 2       (confirm one tick faster)
"""

import random
import statistics
from typing import List, Optional

random.seed(42)

# ─── Simulation ───────────────────────────────────────────────────────────────
N_MARKETS      = 10_000
N_TICKS        = 300
FLIP_PROB      = 0.35
FLIP_WINDOW    = (60, 220)
NOISE_SIGMA    = 0.008
DRIFT_STRENGTH = 0.004
OUTCOME_PRICE  = 0.85
LOSER_PRICE    = 0.15
OPEN_SPREAD    = 0.04

# ─── Shared constants ─────────────────────────────────────────────────────────
ENTRY_MIN      = 0.28
ENTRY_MAX      = 0.60
RUNG_SPREAD    = 0.050
RUNG_STEP      = 0.025
RUNG_USD       = 2.00
MAX_RUNG_PRICE = 0.92
MAX_RUNGS      = 10
LOSS_CAP       = 3.00
MAX_SIDE_COST  = 12.0
MIN_TRADE      = 1.00
EXIT_TTC       = 50
EXIT_MIN_BID   = 0.28
MIN_RR         = 1.5

# ─── Baseline flip constants ──────────────────────────────────────────────────
BASE_FLIP_TRIGGER    = 0.50
BASE_FLIP_CONFIRM    = 3
BASE_RECOVERY_BUDGET = 3.00
BASE_RECOVERY_TARGET = 2.00   # desired net profit after recovering ALL old costs
BASE_FLIP_BUDGET     = 10.00

# ─── Aggressive flip constants (F1 + F2 + F3) ────────────────────────────────
AGG_FLIP_TRIGGER  = 0.44    # [F3] require bigger move before flip considered
AGG_FLIP_CONFIRM  = 2       # [F3] confirm one tick faster
AGG_FLIP_SPREAD   = 0.08    # [F1] sell target = buy_price + this (fixed, not cost-recovery)
AGG_CASCADE_RUNGS = 4       # [F2] place this many rungs immediately on new side
AGG_FLIP_RUNG_USD = 4.00    # [F2] each cascade rung is this many USD (vs RUNG_USD=2)
AGG_FLIP_BUDGET   = 30.00   # [F2] larger total flip budget to accommodate cascade

# ─── Surgical flip constants ──────────────────────────────────────────────────
# Keep detection identical to baseline (no false positives).
# Only fix the sell target formula and give the recovery trade real size.
SURG_FLIP_TRIGGER    = 0.50   # same as baseline
SURG_FLIP_CONFIRM    = 3      # same as baseline
SURG_RECOVERY_BUDGET = 10.00  # 3× bigger → recovery trade actually matters
SURG_FLIP_BUDGET     = 25.00  # enough for 2-3 flips at $10 each
# sell_tgt = buy_px + SURG_SELL_PREMIUM  (reachable fixed spread, sunk costs ignored)
SURG_SELL_PREMIUM    = 0.10   # ~2× RUNG_SPREAD — reliably hittable on a real flip


# ═══════════════════════════════════════════════════════════════════════════════
# Price generator (identical to other MC files)
# ═══════════════════════════════════════════════════════════════════════════════
def generate_market():
    does_flip = random.random() < FLIP_PROB
    if does_flip:
        flip_tick     = random.randint(*FLIP_WINDOW)
        first_winner  = 'UP' if random.random() < 0.5 else 'DOWN'
        second_winner = 'DOWN' if first_winner == 'UP' else 'UP'
        final_outcome = second_winner
    else:
        flip_tick     = N_TICKS + 1
        final_outcome = 'UP' if random.random() < 0.5 else 'DOWN'
        first_winner  = final_outcome
        second_winner = final_outcome

    if first_winner == 'UP':
        up0 = 0.50 + OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 - OPEN_SPREAD / 2 + random.gauss(0, 0.01)
    else:
        up0 = 0.50 - OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 + OPEN_SPREAD / 2 + random.gauss(0, 0.01)

    up_prices = [max(0.05, min(0.95, up0))]
    dn_prices = [max(0.05, min(0.95, dn0))]

    for t in range(1, N_TICKS):
        winner_now = first_winner if t < flip_tick else second_winner
        progress   = t / N_TICKS
        ds         = DRIFT_STRENGTH * (0.5 + progress)
        up_tgt = OUTCOME_PRICE if winner_now == 'UP' else LOSER_PRICE
        dn_tgt = OUTCOME_PRICE if winner_now == 'DOWN' else LOSER_PRICE
        up_new = up_prices[-1] + ds * (up_tgt - up_prices[-1]) + random.gauss(0, NOISE_SIGMA)
        dn_new = dn_prices[-1] + ds * (dn_tgt - dn_prices[-1]) + random.gauss(0, NOISE_SIGMA)
        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.15:
            corr = (mid - 0.5) * 0.3
            up_new -= corr; dn_new -= corr
        up_prices.append(max(0.02, min(0.98, up_new)))
        dn_prices.append(max(0.02, min(0.98, dn_new)))

    return up_prices, dn_prices, final_outcome


# ═══════════════════════════════════════════════════════════════════════════════
# Position helper
# ═══════════════════════════════════════════════════════════════════════════════
class _Pos:
    def __init__(self):
        self.qty = 0.0; self.cost = 0.0
    @property
    def avg(self): return self.cost / self.qty if self.qty > 0.001 else 0.0
    def add(self, qty, price): self.qty += qty; self.cost += qty * price
    def remove(self, qty):
        qty = min(qty, self.qty)
        if self.qty < 0.001: return 0.0
        cb = self.avg * qty
        self.qty -= qty; self.cost -= cb
        if self.qty < 0.01: self.qty = 0.0; self.cost = 0.0
        return cb
    def clear(self): self.qty = 0.0; self.cost = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Shared state + buy/sell logic
# ═══════════════════════════════════════════════════════════════════════════════
class _Core:
    def __init__(self, flip_budget):
        self.cash        = 1000.0
        self.cash_out    = 0.0
        self.cash_in     = 0.0
        self.realised    = 0.0
        self.trade_count = 0
        self._pos        = {'UP': _Pos(), 'DOWN': _Pos()}
        self._rungs: List[dict] = []
        self._phase        = 'scout'
        self._ladder_side  = None
        self._last_rung_px = 0.0
        self._flip_ticks   = 0
        self._flip_budget  = flip_budget
        # A single recovery position (baseline uses this; aggressive uses cascade rungs)
        self._rec_side     = None
        self._rec_qty      = 0.0
        self._rec_target   = 0.0

    def _pnl_if(self, outcome):
        up = self._pos['UP']; dn = self._pos['DOWN']
        payout = up.qty if outcome == 'UP' else dn.qty
        return payout - (up.cost + dn.cost) + self.realised

    def _max_cap(self, side):
        other = 'DOWN' if side == 'UP' else 'UP'
        return max(0.0, self._pnl_if(other) + LOSS_CAP)

    def _avail(self):
        spent = self._pos['UP'].cost + self._pos['DOWN'].cost
        return min(max(0.0, self.cash), max(0.0, 1000.0 - spent))

    def _buy(self, side, price, usd):
        usd = min(usd, self._max_cap(side), self._avail())
        if usd < MIN_TRADE: return None
        price = max(0.04, min(0.96, price))
        qty   = usd / price
        self.cash -= usd; self.cash_out += usd; self.trade_count += 1
        self._pos[side].add(qty, price)
        return (qty, price, usd)

    def _sell(self, side, price, qty):
        qty = min(qty, self._pos[side].qty)
        if qty < 0.01: return None
        price    = max(0.04, min(0.96, price))
        proceeds = qty * price
        cb       = self._pos[side].remove(qty)
        self.cash += proceeds; self.cash_in += proceeds
        self.realised += proceeds - cb; self.trade_count += 1
        return proceeds - cb

    def _open_count(self, side): return sum(1 for r in self._rungs if r['side'] == side)
    def _open_cost(self, side):  return sum(r['cost'] for r in self._rungs if r['side'] == side)
    def _sl_dist(self):          return round(RUNG_SPREAD / MIN_RR, 4)

    def _sell_check(self, price):
        """Check rung sell targets + stops. Returns True if recovery position sold."""
        sold = []
        for r in self._rungs:
            if r.get('hold_to_res'): continue
            px = price[r['side']]
            if px >= r['sell_target'] or px <= r.get('stop_price', 0.0):
                self._sell(r['side'], px, r['qty'])
                sold.append(r)
        for r in sold: self._rungs.remove(r)

        rec_done = False
        if self._rec_side and self._rec_qty > 0.01:
            if price[self._rec_side] >= self._rec_target:
                self._sell(self._rec_side, price[self._rec_side],
                           self._pos[self._rec_side].qty)
                self._rec_qty = 0.0; self._rec_side = None
                rec_done = True
        return rec_done

    def _exit_sell(self, price, bid):
        exited = []
        for r in self._rungs:
            if r.get('hold_to_res'): continue
            b = bid[r['side']]
            if b >= EXIT_MIN_BID:
                self._sell(r['side'], b, r['qty'])
                exited.append(r)
        for r in exited: self._rungs.remove(r)

    def resolve(self, outcome):
        up = self._pos['UP']; dn = self._pos['DOWN']
        payout = up.qty if outcome == 'UP' else dn.qty
        self.realised += payout - (up.cost + dn.cost)
        self.cash += payout; up.clear(); dn.clear(); self._rungs.clear()
        return self.realised


# ═══════════════════════════════════════════════════════════════════════════════
# VARIANT A: BASELINE
# ═══════════════════════════════════════════════════════════════════════════════
def run_baseline(up_prices, dn_prices, outcome):
    c  = _Core(flip_budget=BASE_FLIP_BUDGET)
    sl = c._sl_dist()

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        price   = {'UP': up, 'DOWN': dn}
        bid     = {'UP': round(up - 0.005, 4), 'DOWN': round(dn - 0.005, 4)}
        leading = 'UP' if up >= dn else 'DOWN'

        if ttc < EXIT_TTC: c._exit_sell(price, bid)
        if ttc < 10: continue

        rec_done = c._sell_check(price)

        # ── SCOUT ─────────────────────────────────────────────────────────
        if c._phase == 'scout':
            lp = price[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                r = c._buy(leading, lp, RUNG_USD)
                if r:
                    qty, px, _ = r
                    c._rungs.append({'side': leading, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    c._ladder_side = leading; c._last_rung_px = px; c._phase = 'ladder'
            continue

        # ── shared flip check ──────────────────────────────────────────────
        def do_flip_baseline(ls):
            other    = 'DOWN' if ls == 'UP' else 'UP'
            other_px = price[other]
            for r in c._rungs:
                if r['side'] == ls and not r.get('hold_to_res'):
                    r['hold_to_res'] = True
            open_cost  = c._open_cost(ls)
            net_needed = open_cost + BASE_RECOVERY_TARGET
            rec_bud    = min(BASE_RECOVERY_BUDGET, c._flip_budget, c._avail(),
                             MAX_SIDE_COST - c._pos[other].cost)
            if rec_bud >= MIN_TRADE and other_px > 0.01:
                res = c._buy(other, other_px, rec_bud)
                if res:
                    rqty, rpx, _ = res
                    c._flip_budget -= rqty * rpx
                    # ← THE BROKEN FORMULA (recovering all sunk costs)
                    sell_tgt = min(rpx + net_needed / rqty, MAX_RUNG_PRICE)
                    c._rec_side = other; c._rec_qty = rqty
                    c._rec_target = round(sell_tgt, 4)
                    c._ladder_side = other; c._last_rung_px = other_px
                    c._flip_ticks  = 0; c._phase = 'flip_recover'
                    return True
            return False

        def maybe_flip_base(ls):
            if price[ls] < BASE_FLIP_TRIGGER and leading != ls:
                c._flip_ticks += 1
            else:
                c._flip_ticks = 0
            if c._flip_ticks >= BASE_FLIP_CONFIRM and c._flip_budget >= MIN_TRADE:
                return do_flip_baseline(ls)
            return False

        # ── LADDER ────────────────────────────────────────────────────────
        if c._phase == 'ladder':
            ls = c._ladder_side
            maybe_flip_base(ls)
            if c._phase == 'ladder':
                ls_p = price[ls]; oc = c._open_count(ls); avail = c._avail()
                if (ls_p >= c._last_rung_px + RUNG_STEP and ls_p <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS and avail >= RUNG_USD
                        and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    res = c._buy(ls, ls_p, RUNG_USD)
                    if res:
                        qty, px, _ = res
                        c._rungs.append({'side': ls, 'buy_price': px,
                            'sell_target': round(px + RUNG_SPREAD, 4),
                            'stop_price':  round(px - sl, 4),
                            'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                        c._last_rung_px = px

        # ── FLIP_RECOVER ──────────────────────────────────────────────────
        elif c._phase == 'flip_recover':
            ls = c._ladder_side; maybe_flip_base(ls); ls = c._ladder_side
            ls_p = price[ls]; oc = c._open_count(ls); avail = c._avail()
            if (ls_p >= c._last_rung_px + RUNG_STEP and ls_p <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS and avail >= RUNG_USD
                    and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                res = c._buy(ls, ls_p, RUNG_USD)
                if res:
                    qty, px, _ = res
                    c._rungs.append({'side': ls, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    c._last_rung_px = px
            if rec_done: c._phase = 'ladder'

    return {'pnl': c.resolve(outcome), 'traded': c.trade_count > 0,
            'trade_count': c.trade_count, 'cash_out': c.cash_out}


# ═══════════════════════════════════════════════════════════════════════════════
# VARIANT B: FLIP_AGGRESSIVE (F1 + F2 + F3)
# ═══════════════════════════════════════════════════════════════════════════════
def run_flip_aggressive(up_prices, dn_prices, outcome):
    c  = _Core(flip_budget=AGG_FLIP_BUDGET)
    sl = c._sl_dist()

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        price   = {'UP': up, 'DOWN': dn}
        bid     = {'UP': round(up - 0.005, 4), 'DOWN': round(dn - 0.005, 4)}
        leading = 'UP' if up >= dn else 'DOWN'

        if ttc < EXIT_TTC: c._exit_sell(price, bid)
        if ttc < 10: continue

        rec_done = c._sell_check(price)

        # ── SCOUT ─────────────────────────────────────────────────────────
        if c._phase == 'scout':
            lp = price[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                r = c._buy(leading, lp, RUNG_USD)
                if r:
                    qty, px, _ = r
                    c._rungs.append({'side': leading, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    c._ladder_side = leading; c._last_rung_px = px; c._phase = 'ladder'
            continue

        # ── Aggressive flip ────────────────────────────────────────────────
        def do_flip_aggressive(ls):
            """
            F1: sunk costs are gone — sell old rungs at bid if possible,
                mark rest hold_to_res. Set fixed-spread sell target on new pos.
            F2: immediately cascade CASCADE_RUNGS rungs on the new side.
            """
            other = 'DOWN' if ls == 'UP' else 'UP'

            # Sell old rungs at current bid if bid is meaningful (≥ 0.08)
            to_remove = []
            for r in c._rungs:
                if r['side'] == ls and not r.get('hold_to_res'):
                    b = bid[ls]
                    if b >= 0.08:
                        c._sell(ls, b, r['qty'])
                        to_remove.append(r)
                    else:
                        r['hold_to_res'] = True
            for r in to_remove:
                if r in c._rungs: c._rungs.remove(r)

            # F2: cascade — buy CASCADE_RUNGS rungs spread RUNG_STEP apart
            base_px = price[other]
            placed  = 0
            step_px = base_px
            for i in range(AGG_CASCADE_RUNGS):
                if step_px > MAX_RUNG_PRICE: break
                if c._flip_budget < AGG_FLIP_RUNG_USD: break
                if c._pos[other].cost + AGG_FLIP_RUNG_USD > MAX_SIDE_COST: break
                res = c._buy(other, step_px, AGG_FLIP_RUNG_USD)
                if res:
                    qty, px, spent = res
                    c._flip_budget -= spent
                    # F1: fixed-spread target — not tied to old rung costs
                    tgt = round(px + AGG_FLIP_SPREAD, 4)
                    c._rungs.append({'side': other, 'buy_price': px,
                        'sell_target': tgt,
                        'stop_price':  round(px - AGG_FLIP_SPREAD / 2, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    placed += 1
                    step_px = round(step_px + RUNG_STEP, 4)
                else:
                    break

            if placed > 0:
                c._ladder_side  = other
                c._last_rung_px = price[other]
                c._flip_ticks   = 0
                c._phase        = 'flip_recover'
                return True
            return False

        def maybe_flip_agg(ls):
            # F3: lower trigger + faster confirm
            if price[ls] < AGG_FLIP_TRIGGER and leading != ls:
                c._flip_ticks += 1
            else:
                c._flip_ticks = 0
            if c._flip_ticks >= AGG_FLIP_CONFIRM and c._flip_budget >= AGG_FLIP_RUNG_USD:
                return do_flip_aggressive(ls)
            return False

        # ── LADDER ────────────────────────────────────────────────────────
        if c._phase == 'ladder':
            ls = c._ladder_side
            maybe_flip_agg(ls)
            if c._phase == 'ladder':
                ls_p = price[ls]; oc = c._open_count(ls); avail = c._avail()
                if (ls_p >= c._last_rung_px + RUNG_STEP and ls_p <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS and avail >= RUNG_USD
                        and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    res = c._buy(ls, ls_p, RUNG_USD)
                    if res:
                        qty, px, _ = res
                        c._rungs.append({'side': ls, 'buy_price': px,
                            'sell_target': round(px + RUNG_SPREAD, 4),
                            'stop_price':  round(px - sl, 4),
                            'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                        c._last_rung_px = px

        # ── FLIP_RECOVER ──────────────────────────────────────────────────
        elif c._phase == 'flip_recover':
            ls = c._ladder_side; maybe_flip_agg(ls); ls = c._ladder_side
            ls_p = price[ls]; oc = c._open_count(ls); avail = c._avail()
            if (ls_p >= c._last_rung_px + RUNG_STEP and ls_p <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS and avail >= RUNG_USD
                    and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                res = c._buy(ls, ls_p, RUNG_USD)
                if res:
                    qty, px, _ = res
                    c._rungs.append({'side': ls, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    c._last_rung_px = px
            if rec_done: c._phase = 'ladder'

    return {'pnl': c.resolve(outcome), 'traded': c.trade_count > 0,
            'trade_count': c.trade_count, 'cash_out': c.cash_out}


# ═══════════════════════════════════════════════════════════════════════════════
# VARIANT C: SURGICAL  (same detection, fixed sell target, bigger recovery budget)
# ═══════════════════════════════════════════════════════════════════════════════
def run_surgical(up_prices, dn_prices, outcome):
    c  = _Core(flip_budget=SURG_FLIP_BUDGET)
    sl = c._sl_dist()

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        price   = {'UP': up, 'DOWN': dn}
        bid     = {'UP': round(up - 0.005, 4), 'DOWN': round(dn - 0.005, 4)}
        leading = 'UP' if up >= dn else 'DOWN'

        if ttc < EXIT_TTC: c._exit_sell(price, bid)
        if ttc < 10: continue

        rec_done = c._sell_check(price)

        # ── SCOUT ─────────────────────────────────────────────────────────
        if c._phase == 'scout':
            lp = price[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                r = c._buy(leading, lp, RUNG_USD)
                if r:
                    qty, px, _ = r
                    c._rungs.append({'side': leading, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    c._ladder_side = leading; c._last_rung_px = px; c._phase = 'ladder'
            continue

        def do_flip_surgical(ls):
            other    = 'DOWN' if ls == 'UP' else 'UP'
            other_px = price[other]
            # Keep old rungs as hold_to_res — but DON'T try to recover their cost
            for r in c._rungs:
                if r['side'] == ls and not r.get('hold_to_res'):
                    r['hold_to_res'] = True
            rec_bud = min(SURG_RECOVERY_BUDGET, c._flip_budget, c._avail(),
                          MAX_SIDE_COST - c._pos[other].cost)
            if rec_bud >= MIN_TRADE and other_px > 0.01:
                res = c._buy(other, other_px, rec_bud)
                if res:
                    rqty, rpx, spent = res
                    c._flip_budget -= spent
                    # ← THE FIX: fixed reachable premium, sunk costs ignored
                    sell_tgt = min(rpx + SURG_SELL_PREMIUM, MAX_RUNG_PRICE)
                    c._rec_side   = other; c._rec_qty = rqty
                    c._rec_target = round(sell_tgt, 4)
                    c._ladder_side = other; c._last_rung_px = other_px
                    c._flip_ticks  = 0; c._phase = 'flip_recover'
                    return True
            return False

        def maybe_flip_surg(ls):
            if price[ls] < SURG_FLIP_TRIGGER and leading != ls:
                c._flip_ticks += 1
            else:
                c._flip_ticks = 0
            if c._flip_ticks >= SURG_FLIP_CONFIRM and c._flip_budget >= MIN_TRADE:
                return do_flip_surgical(ls)
            return False

        # ── LADDER ────────────────────────────────────────────────────────
        if c._phase == 'ladder':
            ls = c._ladder_side
            maybe_flip_surg(ls)
            if c._phase == 'ladder':
                ls_p = price[ls]; oc = c._open_count(ls); avail = c._avail()
                if (ls_p >= c._last_rung_px + RUNG_STEP and ls_p <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS and avail >= RUNG_USD
                        and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    res = c._buy(ls, ls_p, RUNG_USD)
                    if res:
                        qty, px, _ = res
                        c._rungs.append({'side': ls, 'buy_price': px,
                            'sell_target': round(px + RUNG_SPREAD, 4),
                            'stop_price':  round(px - sl, 4),
                            'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                        c._last_rung_px = px

        # ── FLIP_RECOVER ──────────────────────────────────────────────────
        elif c._phase == 'flip_recover':
            ls = c._ladder_side; maybe_flip_surg(ls); ls = c._ladder_side
            ls_p = price[ls]; oc = c._open_count(ls); avail = c._avail()
            if (ls_p >= c._last_rung_px + RUNG_STEP and ls_p <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS and avail >= RUNG_USD
                    and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                res = c._buy(ls, ls_p, RUNG_USD)
                if res:
                    qty, px, _ = res
                    c._rungs.append({'side': ls, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False})
                    c._last_rung_px = px
            if rec_done: c._phase = 'ladder'

    return {'pnl': c.resolve(outcome), 'traded': c.trade_count > 0,
            'trade_count': c.trade_count, 'cash_out': c.cash_out}


# ═══════════════════════════════════════════════════════════════════════════════
# Stats
# ═══════════════════════════════════════════════════════════════════════════════
def percentile(data, p):
    if not data: return 0.0
    s = sorted(data); idx = (len(s) - 1) * p / 100
    lo = int(idx); hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)

def pct(x, n): return f'{100*x/n:.1f}%' if n else '0.0%'


def print_stats(label, results):
    traded    = [r for r in results if r['traded']]
    wins      = [r for r in traded if r['pnl'] > 0]
    losses    = [r for r in traded if r['pnl'] < 0]
    n         = len(traded)
    all_pnls  = [r['pnl'] for r in traded]
    win_pnls  = [r['pnl'] for r in wins]
    loss_pnls = [r['pnl'] for r in losses]
    tcs       = [r['trade_count'] for r in traded]
    total_pnl = sum(all_pnls)
    ev        = total_pnl / n if n else 0.0

    sep = '─' * 60
    print(f'\n  ┌{sep}┐')
    print(f'  │  {label:<57}│')
    print(f'  ├{sep}┤')
    print(f'  │  Traded        : {n:>6,} / {len(results):,} ({pct(n,len(results)):>6}){"":>14}│')
    print(f'  │  Winrate       : {pct(len(wins),n):>6}  ({len(wins):,}W / {len(losses):,}L){"":>17}│')
    print(f'  │  Total PnL     : {total_pnl:>+10.2f} USD{"":>29}│')
    print(f'  │  EV/market     : {ev:>+10.4f} USD{"":>29}│')
    if all_pnls:
        print(f'  │  Median PnL    : {statistics.median(all_pnls):>+10.4f} USD{"":>29}│')
        print(f'  │  Stdev         : {statistics.stdev(all_pnls):>10.4f} USD{"":>29}│')
        print(f'  │  Best / Worst  : {max(all_pnls):>+8.4f} / {min(all_pnls):>+8.4f} USD{"":>14}│')
        print(f'  │  P10 / P90     : {percentile(all_pnls,10):>+8.4f} / {percentile(all_pnls,90):>+8.4f} USD{"":>14}│')
    if win_pnls and loss_pnls:
        avg_w = statistics.mean(win_pnls); avg_l = abs(statistics.mean(loss_pnls))
        wl    = avg_w / avg_l
        w     = len(wins) / n
        kelly = w - (1 - w) / wl
        print(f'  │  Avg win/loss  : {avg_w:>+8.4f} / {statistics.mean(loss_pnls):>+8.4f} USD{"":>14}│')
        print(f'  │  Win/Loss ratio: {wl:>10.3f}x{"":>32}│')
        print(f'  │  Kelly         : {kelly:>+10.3f}{"":>38}│')
    if tcs:
        print(f'  │  Avg trades    : {statistics.mean(tcs):>10.1f}{"":>32}│')
    print(f'  └{sep}┘')

    bins = [
        ('< -5',    lambda p: p < -5.00),
        ('-5 to -3',lambda p: -5.00 <= p < -3.00),
        ('-3 to -1',lambda p: -3.00 <= p < -1.00),
        ('-1 to 0', lambda p: -1.00 <= p <  0.00),
        ('0 to +1', lambda p:  0.00 <= p <= 1.00),
        ('+1 to +3',lambda p:  1.00 <  p <= 3.00),
        ('+3 to +5',lambda p:  3.00 <  p <= 5.00),
        ('> +5',    lambda p: p >  5.00),
    ]
    bw = 28
    print(f'  {"Bucket":>10}  {"":28}  count')
    for lbl, cond in bins:
        cnt = sum(1 for p in all_pnls if cond(p))
        bar = '█' * int(bw * cnt / n) if n else ''
        print(f'  {lbl:>10}  {bar:<{bw}}  {cnt:>5} ({pct(cnt,n):>5})')


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def delta_row(label, a_res, b_res):
    a = [r['pnl'] for r in a_res if r['traded']]
    b = [r['pnl'] for r in b_res if r['traded']]
    a_w = sum(1 for p in a if p > 0); b_w = sum(1 for p in b if p > 0)
    dev  = (sum(a)/len(a) if a else 0) - (sum(b)/len(b) if b else 0)
    dwr  = (a_w/len(a)*100 if a else 0) - (b_w/len(b)*100 if b else 0)
    dtot = sum(a) - sum(b)
    print(f'  {label:<42}  EV {dev:>+7.4f}  WR {dwr:>+6.2f}pp  Tot {dtot:>+8.2f}')


def main():
    print(f'Generating {N_MARKETS:,} price paths...')
    markets = [generate_market() for _ in range(N_MARKETS)]
    print('Running BASELINE + FLIP_AGGRESSIVE + SURGICAL on same paths...\n')

    base_res = []
    agg_res  = []
    surg_res = []

    for i, (up_p, dn_p, outcome) in enumerate(markets):
        if i % 2000 == 0 and i > 0:
            print(f'  ... {i:,} / {N_MARKETS:,}', flush=True)
        base_res.append(run_baseline(up_p, dn_p, outcome))
        agg_res.append(run_flip_aggressive(up_p, dn_p, outcome))
        surg_res.append(run_surgical(up_p, dn_p, outcome))

    sep = '═' * 62
    print(f'\n{sep}')
    print('  LADDERMATE FLIP A/B/C  —  10 000 markets, same price paths')
    print(f'{sep}')
    print(f'\n  A  Baseline:   trigger={BASE_FLIP_TRIGGER} confirm={BASE_FLIP_CONFIRM}'
          f'  rec=${BASE_RECOVERY_BUDGET}  budget=${BASE_FLIP_BUDGET}')
    print(f'     sell_tgt = buy_px + (open_cost + {BASE_RECOVERY_TARGET}) / qty  [broken]')
    print(f'  B  Aggressive: trigger={AGG_FLIP_TRIGGER} confirm={AGG_FLIP_CONFIRM}'
          f'  cascade={AGG_CASCADE_RUNGS}×${AGG_FLIP_RUNG_USD}  budget=${AGG_FLIP_BUDGET}')
    print(f'     sell_tgt = buy_px + {AGG_FLIP_SPREAD}  (fixed, sunk cost ignored)')
    print(f'  C  Surgical:   trigger={SURG_FLIP_TRIGGER} confirm={SURG_FLIP_CONFIRM}'
          f'  rec=${SURG_RECOVERY_BUDGET}  budget=${SURG_FLIP_BUDGET}')
    print(f'     sell_tgt = buy_px + {SURG_SELL_PREMIUM}  (fixed premium, 2× spread)')

    print_stats('A — BASELINE  (broken recovery sell target)', base_res)
    print()
    print_stats('B — FLIP_AGGRESSIVE  (cascade + faster detect + fixed spread)', agg_res)
    print()
    print_stats('C — SURGICAL  (same detection + fixed sell target + bigger budget)', surg_res)

    print(f'\n{sep}')
    print('  DELTA vs BASELINE')
    print(sep)
    delta_row('B  Flip_Aggressive − Baseline', agg_res,  base_res)
    delta_row('C  Surgical        − Baseline', surg_res, base_res)
    print(f'{sep}')


if __name__ == '__main__':
    main()
