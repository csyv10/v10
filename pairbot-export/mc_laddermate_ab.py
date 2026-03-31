"""
mc_laddermate_ab.py
════════════════════════════════════════════════════════════════════════════
A/B test: baseline LadderMate vs. 4 proposed improvements (10 000 markets).

BASELINE  — exact mirror of LadderMateStrategy current config
IMPROVED  — same core, plus:
  1. ENTRY_CONFIRM_TICKS=5 + gap≥0.03  (filter coinflip markets)
  2. Sell hold_to_res rungs immediately on flip if bid≥0.10
     (recover capital instead of gambling on reversal)
  3. Pyramid sizing: RUNG_USD * (1 + 0.3 * rung_index)
     (bigger bets on proven trend)
  4. Re-entry confirmation after recovery sell: require N ticks again
     (don't auto-restart ladder blindly)
"""

import random
import statistics
from typing import List, Optional

random.seed(42)

# ─── Simulation parameters ────────────────────────────────────────────────────
N_MARKETS      = 10_000
N_TICKS        = 300
FLIP_PROB      = 0.35
FLIP_WINDOW    = (60, 220)
NOISE_SIGMA    = 0.008
DRIFT_STRENGTH = 0.004
OUTCOME_PRICE  = 0.85
LOSER_PRICE    = 0.15
OPEN_SPREAD    = 0.04

# ─── Shared strategy constants (both variants) ────────────────────────────────
ENTRY_MIN       = 0.28
ENTRY_MAX       = 0.60
RUNG_SPREAD     = 0.050
RUNG_STEP       = 0.025
RUNG_USD        = 2.00
MAX_RUNG_PRICE  = 0.92
MAX_RUNGS       = 10
FLIP_TRIGGER    = 0.50
FLIP_CONFIRM    = 3
RECOVERY_BUDGET = 3.00
RECOVERY_TARGET = 2.00
MAX_SIDE_COST   = 12.0
LOSS_CAP        = 3.00
MIN_TRADE       = 1.00
FLIP_BUDGET_G   = 10.00
EXIT_TTC        = 50
EXIT_MIN_BID    = 0.28
MIN_RR          = 1.5

# ─── Improvement-specific constants ───────────────────────────────────────────
ENTRY_CONFIRM_TICKS = 5     # [1] min consecutive ticks leading side must lead
ENTRY_GAP_MIN       = 0.03  # [1] min price gap between sides at entry
FLIP_SELL_BID_MIN   = 0.10  # [2] sell hold_to_res rungs if bid >= this on flip
PYRAMID_FACTOR      = 0.30  # [3] RUNG_USD * (1 + PYRAMID_FACTOR * rung_index)
REENTRY_CONFIRM     = 5     # [4] ticks required to re-enter after recovery sell


# ═══════════════════════════════════════════════════════════════════════════════
# Price path generator
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
        self.qty  = 0.0
        self.cost = 0.0

    @property
    def avg(self):
        return self.cost / self.qty if self.qty > 0.001 else 0.0

    def add(self, qty, price):
        self.qty  += qty
        self.cost += qty * price

    def remove(self, qty):
        qty = min(qty, self.qty)
        if self.qty < 0.001:
            return 0.0
        cb = self.avg * qty
        self.qty  -= qty
        self.cost -= cb
        if self.qty < 0.01:
            self.qty = 0.0; self.cost = 0.0
        return cb

    def clear(self):
        self.qty = 0.0; self.cost = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Shared core — used by both variants
# ═══════════════════════════════════════════════════════════════════════════════
class _Core:
    def __init__(self):
        self.cash         = 1000.0
        self.cash_out     = 0.0
        self.cash_in      = 0.0
        self.realised     = 0.0
        self.trade_count  = 0
        self._pos         = {'UP': _Pos(), 'DOWN': _Pos()}
        self._rungs: List[dict] = []
        self._phase        = 'scout'
        self._ladder_side  = None
        self._last_rung_px = 0.0
        self._flip_ticks   = 0
        self._flip_budget  = FLIP_BUDGET_G
        self._rec_side     = None
        self._rec_qty      = 0.0
        self._rec_target   = 0.0
        self._ask          = {'UP': 0.5, 'DOWN': 0.5}
        self._bid          = {'UP': 0.495, 'DOWN': 0.495}

    # ── accounting ──────────────────────────────────────────────────────────
    def _pnl_if(self, outcome):
        up = self._pos['UP']
        dn = self._pos['DOWN']
        payout = up.qty if outcome == 'UP' else dn.qty
        return payout - (up.cost + dn.cost) + self.realised

    def _max_cap(self, side, price):
        other = 'DOWN' if side == 'UP' else 'UP'
        return max(0.0, self._pnl_if(other) + LOSS_CAP)

    def _avail(self):
        spent = self._pos['UP'].cost + self._pos['DOWN'].cost
        return min(max(0.0, self.cash), max(0.0, 1000.0 - spent))

    def _buy(self, side, price, usd):
        usd = min(usd, self._max_cap(side, price), self._avail())
        if usd < MIN_TRADE:
            return None
        price = max(0.04, min(0.96, price))
        qty   = usd / price
        self.cash     -= usd
        self.cash_out += usd
        self.trade_count += 1
        self._pos[side].add(qty, price)
        return (qty, price, usd)

    def _sell(self, side, price, qty):
        qty  = min(qty, self._pos[side].qty)
        if qty < 0.01:
            return None
        price    = max(0.04, min(0.96, price))
        proceeds = qty * price
        cb       = self._pos[side].remove(qty)
        self.cash         += proceeds
        self.cash_in      += proceeds
        self.realised     += proceeds - cb
        self.trade_count  += 1
        return proceeds - cb

    def _open_count(self, side):
        return sum(1 for r in self._rungs if r['side'] == side)

    def _open_cost(self, side):
        return sum(r['cost'] for r in self._rungs if r['side'] == side)

    def _sl_dist(self):
        return round(RUNG_SPREAD / MIN_RR, 4)

    # ── common sell-check (both variants call this) ──────────────────────────
    def _sell_check(self, price):
        sold = []
        for r in self._rungs:
            if r.get('hold_to_res'):
                continue
            s  = r['side']
            px = price[s]
            if px >= r['sell_target']:
                self._sell(s, px, r['qty'])
                sold.append(r)
            elif px <= r.get('stop_price', 0.0):
                self._sell(s, px, r['qty'])
                sold.append(r)
        for r in sold:
            self._rungs.remove(r)

        # recovery sell
        if self._rec_side and self._rec_qty > 0.01:
            if price[self._rec_side] >= self._rec_target:
                self._sell(self._rec_side, price[self._rec_side],
                           self._pos[self._rec_side].qty)
                self._rec_qty  = 0.0
                self._rec_side = None
                return True   # signal: recovery done
        return False

    # ── exit window sell ─────────────────────────────────────────────────────
    def _exit_sell(self, price, bid):
        exited = []
        for r in self._rungs:
            if r.get('hold_to_res'):
                continue
            s   = r['side']
            bpx = bid[s]
            if bpx < EXIT_MIN_BID:
                continue
            self._sell(s, bpx, r['qty'])
            exited.append(r)
        for r in exited:
            self._rungs.remove(r)

    # ── resolution ──────────────────────────────────────────────────────────
    def resolve(self, outcome):
        up = self._pos['UP']
        dn = self._pos['DOWN']
        payout = up.qty if outcome == 'UP' else dn.qty
        cost   = up.cost + dn.cost
        self.realised += payout - cost
        self.cash     += payout
        up.clear(); dn.clear()
        self._rungs.clear()
        return self.realised


# ═══════════════════════════════════════════════════════════════════════════════
# VARIANT A: BASELINE  (mirrors current LadderMateStrategy exactly)
# ═══════════════════════════════════════════════════════════════════════════════
def run_baseline(up_prices, dn_prices, outcome):
    c = _Core()
    sl = c._sl_dist()

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc  = N_TICKS - t
        price = {'UP': up, 'DOWN': dn}
        bid   = {'UP': round(up - 0.005, 4), 'DOWN': round(dn - 0.005, 4)}
        c._ask = {'UP': up, 'DOWN': dn}
        c._bid = bid
        leading = 'UP' if up >= dn else 'DOWN'
        avail   = c._avail()

        if ttc < EXIT_TTC:
            c._exit_sell(price, bid)
        if ttc < 10:
            continue

        rec_done = c._sell_check(price)

        if c._phase == 'scout':
            lead_p = price[leading]
            if ENTRY_MIN <= lead_p <= ENTRY_MAX:
                t_res = c._buy(leading, lead_p, RUNG_USD)
                if t_res:
                    qty, px, cost = t_res
                    c._rungs.append({
                        'side': leading, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False,
                    })
                    c._ladder_side   = leading
                    c._last_rung_px  = px
                    c._phase         = 'ladder'
            continue

        # ── flip logic (shared between ladder and flip_recover) ──────────────
        def maybe_flip(ls):
            other = 'DOWN' if ls == 'UP' else 'UP'
            px_ls = price[ls]
            if px_ls < FLIP_TRIGGER and leading != ls:
                c._flip_ticks += 1
            else:
                c._flip_ticks = 0

            if c._flip_ticks >= FLIP_CONFIRM and c._flip_budget >= MIN_TRADE:
                # mark old rungs hold_to_res (baseline: keep, don't sell)
                for r in c._rungs:
                    if r['side'] == ls and not r.get('hold_to_res'):
                        r['hold_to_res'] = True

                open_cost  = c._open_cost(ls)
                net_needed = open_cost + RECOVERY_TARGET
                rec_bud    = min(RECOVERY_BUDGET, c._flip_budget, c._avail(),
                                 MAX_SIDE_COST - c._pos[other].cost)
                other_px   = price[other]
                if rec_bud >= MIN_TRADE and other_px > 0.01:
                    res = c._buy(other, other_px, rec_bud)
                    if res:
                        rqty, rpx, _ = res
                        c._flip_budget -= rqty * rpx
                        sell_tgt = min(rpx + net_needed / rqty, MAX_RUNG_PRICE)
                        c._rec_side   = other
                        c._rec_qty    = rqty
                        c._rec_target = round(sell_tgt, 4)
                        c._ladder_side  = other
                        c._last_rung_px = other_px
                        c._flip_ticks   = 0
                        c._phase        = 'flip_recover'
                        return True
            return False

        if c._phase == 'ladder':
            ls = c._ladder_side
            if maybe_flip(ls):
                ls = c._ladder_side
            if c._phase == 'ladder':
                ls_p = price[ls]
                oc   = c._open_count(ls)
                if (ls_p >= c._last_rung_px + RUNG_STEP
                        and ls_p <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS
                        and avail >= RUNG_USD
                        and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    res = c._buy(ls, ls_p, RUNG_USD)
                    if res:
                        qty, px, _ = res
                        c._rungs.append({
                            'side': ls, 'buy_price': px,
                            'sell_target': round(px + RUNG_SPREAD, 4),
                            'stop_price':  round(px - sl, 4),
                            'qty': qty, 'cost': qty * px, 'hold_to_res': False,
                        })
                        c._last_rung_px = px

        elif c._phase == 'flip_recover':
            ls = c._ladder_side
            maybe_flip(ls)
            ls    = c._ladder_side
            ls_p  = price[ls]
            oc    = c._open_count(ls)
            avail = c._avail()
            if (ls_p >= c._last_rung_px + RUNG_STEP
                    and ls_p <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS
                    and avail >= RUNG_USD
                    and c._pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                res = c._buy(ls, ls_p, RUNG_USD)
                if res:
                    qty, px, _ = res
                    c._rungs.append({
                        'side': ls, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False,
                    })
                    c._last_rung_px = px
            if rec_done:
                c._phase = 'ladder'

    return {
        'pnl':         c.resolve(outcome),
        'traded':      c.trade_count > 0,
        'trade_count': c.trade_count,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# VARIANT B: IMPROVED (all 4 changes)
# ═══════════════════════════════════════════════════════════════════════════════
def run_improved(up_prices, dn_prices, outcome):
    c = _Core()
    sl = c._sl_dist()

    # [1] Entry confirmation state
    _confirm_ticks = 0
    _confirm_side: Optional[str] = None

    # [4] Re-entry confirmation after recovery
    _reentry_ticks = 0
    _reentry_needed = False

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc   = N_TICKS - t
        price = {'UP': up, 'DOWN': dn}
        bid   = {'UP': round(up - 0.005, 4), 'DOWN': round(dn - 0.005, 4)}
        c._ask = {'UP': up, 'DOWN': dn}
        c._bid = bid
        leading = 'UP' if up >= dn else 'DOWN'
        gap     = abs(up - dn)
        avail   = c._avail()

        if ttc < EXIT_TTC:
            c._exit_sell(price, bid)
        if ttc < 10:
            continue

        rec_done = c._sell_check(price)

        # ── [1] Entry confirmation counter ────────────────────────────────────
        if c._phase == 'scout':
            lead_p = price[leading]
            if ENTRY_MIN <= lead_p <= ENTRY_MAX:
                if leading == _confirm_side and gap >= ENTRY_GAP_MIN:
                    _confirm_ticks += 1
                else:
                    _confirm_side  = leading
                    _confirm_ticks = 1

                if _confirm_ticks >= ENTRY_CONFIRM_TICKS:
                    t_res = c._buy(leading, lead_p, RUNG_USD)
                    if t_res:
                        qty, px, cost = t_res
                        c._rungs.append({
                            'side': leading, 'buy_price': px,
                            'sell_target': round(px + RUNG_SPREAD, 4),
                            'stop_price':  round(px - sl, 4),
                            'qty': qty, 'cost': qty * px, 'hold_to_res': False,
                        })
                        c._ladder_side   = leading
                        c._last_rung_px  = px
                        c._phase         = 'ladder'
                        _confirm_ticks   = 0
            else:
                _confirm_ticks = 0
                _confirm_side  = None
            continue

        # ── flip logic ────────────────────────────────────────────────────────
        def maybe_flip_imp(ls):
            other   = 'DOWN' if ls == 'UP' else 'UP'
            px_ls   = price[ls]
            if px_ls < FLIP_TRIGGER and leading != ls:
                c._flip_ticks += 1
            else:
                c._flip_ticks = 0

            if c._flip_ticks >= FLIP_CONFIRM and c._flip_budget >= MIN_TRADE:
                # [2] Sell hold_to_res rungs immediately if bid is reasonable
                newly_to_hold = []
                for r in c._rungs:
                    if r['side'] == ls and not r.get('hold_to_res'):
                        b = bid[ls]
                        if b >= FLIP_SELL_BID_MIN:
                            # Sell it immediately — recover capital
                            c._sell(ls, b, r['qty'])
                            newly_to_hold.append(r)  # mark for removal
                        else:
                            r['hold_to_res'] = True
                for r in newly_to_hold:
                    if r in c._rungs:
                        c._rungs.remove(r)

                open_cost  = c._open_cost(ls)
                net_needed = open_cost + RECOVERY_TARGET
                rec_bud    = min(RECOVERY_BUDGET, c._flip_budget, c._avail(),
                                 MAX_SIDE_COST - c._pos[other].cost)
                other_px = price[other]
                if rec_bud >= MIN_TRADE and other_px > 0.01:
                    res = c._buy(other, other_px, rec_bud)
                    if res:
                        rqty, rpx, _ = res
                        c._flip_budget -= rqty * rpx
                        sell_tgt = min(rpx + max(net_needed, RECOVERY_TARGET) / rqty,
                                       MAX_RUNG_PRICE)
                        c._rec_side   = other
                        c._rec_qty    = rqty
                        c._rec_target = round(sell_tgt, 4)
                        c._ladder_side  = other
                        c._last_rung_px = other_px
                        c._flip_ticks   = 0
                        c._phase        = 'flip_recover'
                        return True
            return False

        # [3] Pyramid sizing: rung_usd grows with confirmed rung count
        def pyramid_usd(side):
            idx = c._open_count(side)  # 0-based index of next rung
            return RUNG_USD * (1.0 + PYRAMID_FACTOR * idx)

        if c._phase == 'ladder':
            ls = c._ladder_side
            maybe_flip_imp(ls)
            if c._phase == 'ladder':
                ls_p   = price[ls]
                oc     = c._open_count(ls)
                r_usd  = pyramid_usd(ls)   # [3]
                avail  = c._avail()
                if (ls_p >= c._last_rung_px + RUNG_STEP
                        and ls_p <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS
                        and avail >= r_usd
                        and c._pos[ls].cost + r_usd <= MAX_SIDE_COST):
                    res = c._buy(ls, ls_p, r_usd)
                    if res:
                        qty, px, _ = res
                        c._rungs.append({
                            'side': ls, 'buy_price': px,
                            'sell_target': round(px + RUNG_SPREAD, 4),
                            'stop_price':  round(px - sl, 4),
                            'qty': qty, 'cost': qty * px, 'hold_to_res': False,
                        })
                        c._last_rung_px = px

        elif c._phase == 'flip_recover':
            ls = c._ladder_side
            maybe_flip_imp(ls)
            ls    = c._ladder_side
            ls_p  = price[ls]
            oc    = c._open_count(ls)
            avail = c._avail()
            r_usd = pyramid_usd(ls)   # [3]

            if (ls_p >= c._last_rung_px + RUNG_STEP
                    and ls_p <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS
                    and avail >= r_usd
                    and c._pos[ls].cost + r_usd <= MAX_SIDE_COST):
                res = c._buy(ls, ls_p, r_usd)
                if res:
                    qty, px, _ = res
                    c._rungs.append({
                        'side': ls, 'buy_price': px,
                        'sell_target': round(px + RUNG_SPREAD, 4),
                        'stop_price':  round(px - sl, 4),
                        'qty': qty, 'cost': qty * px, 'hold_to_res': False,
                    })
                    c._last_rung_px = px

            # [4] Re-entry after recovery: need confirmation before new entries
            if rec_done:
                _reentry_needed = True
                _reentry_ticks  = 0
                c._phase = 'reentry_scout'

        elif c._phase == 'reentry_scout':
            # [4] Wait for REENTRY_CONFIRM ticks of consistent lead + gap
            lead_p = price[leading]
            if leading == c._ladder_side and gap >= ENTRY_GAP_MIN:
                _reentry_ticks += 1
            else:
                _reentry_ticks = 0
            if _reentry_ticks >= REENTRY_CONFIRM:
                c._phase       = 'ladder'
                _reentry_ticks = 0

    return {
        'pnl':         c.resolve(outcome),
        'traded':      c.trade_count > 0,
        'trade_count': c.trade_count,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Stats helpers
# ═══════════════════════════════════════════════════════════════════════════════
def percentile(data, p):
    if not data:
        return 0.0
    s   = sorted(data)
    idx = (len(s) - 1) * p / 100
    lo  = int(idx)
    hi  = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)

def pct(x, tot): return f'{100 * x / tot:.1f}%' if tot else '0.0%'


def print_stats(label, results):
    traded  = [r for r in results if r['traded']]
    wins    = [r for r in traded if r['pnl'] > 0]
    losses  = [r for r in traded if r['pnl'] < 0]
    n       = len(traded)
    tot     = len(results)

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
    print(f'  │  Traded        : {n:>6,} / {tot:,} ({pct(n, tot):>6}){"":>19}│')
    print(f'  │  Winrate       : {pct(len(wins), n):>6}  ({len(wins):,} wins / {len(losses):,} losses){"":>12}│')
    print(f'  │  Total PnL     : {total_pnl:>+10.2f} USD{"":>29}│')
    print(f'  │  EV/market     : {ev:>+10.4f} USD{"":>29}│')
    if all_pnls:
        print(f'  │  Median PnL    : {statistics.median(all_pnls):>+10.4f} USD{"":>29}│')
        print(f'  │  Stdev         : {statistics.stdev(all_pnls):>10.4f} USD{"":>29}│')
        print(f'  │  Best          : {max(all_pnls):>+10.4f} USD{"":>29}│')
        print(f'  │  Worst         : {min(all_pnls):>+10.4f} USD{"":>29}│')
        print(f'  │  P10 / P90     : {percentile(all_pnls,10):>+8.4f} / {percentile(all_pnls,90):>+8.4f} USD{"":>14}│')
    if win_pnls and loss_pnls:
        avg_w = statistics.mean(win_pnls)
        avg_l = abs(statistics.mean(loss_pnls))
        wl    = avg_w / avg_l
        w     = len(wins) / n
        kelly = w - (1 - w) / wl
        print(f'  │  Avg win/loss  : {avg_w:>+8.4f} / {statistics.mean(loss_pnls):>+8.4f} USD{"":>14}│')
        print(f'  │  Win/Loss ratio: {wl:>10.3f}x{"":>32}│')
        print(f'  │  Kelly         : {kelly:>+10.3f}  (>0 = edge){"":>21}│')
    if tcs:
        print(f'  │  Avg trades    : {statistics.mean(tcs):>10.1f}{"":>32}│')
    print(f'  └{sep}┘')

    # histogram
    bins = [
        ('< -5',    lambda p: p < -5.00),
        ('-5 to -3',lambda p: -5.00 <= p < -3.00),
        ('-3 to -1',lambda p: -3.00 <= p < -1.00),
        ('-1 to 0', lambda p: -1.00 <= p <  0.00),
        ('0 to +1', lambda p:  0.00 <= p <= 1.00),
        ('+1 to +3',lambda p:  1.00 <  p <= 3.00),
        ('+3 to +5',lambda p:  3.00 <  p <= 5.00),
        ('> +5',    lambda p: p > 5.00),
    ]
    bar_w = 28
    print(f'  {"PnL bucket":>10}  {"":28}  count')
    for lbl, cond in bins:
        cnt = sum(1 for p in all_pnls if cond(p))
        bar = '█' * int(bar_w * cnt / n) if n else ''
        print(f'  {lbl:>10}  {bar:<{bar_w}}  {cnt:>5} ({pct(cnt, n):>5})')


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    print(f'Generating {N_MARKETS:,} price paths...')
    markets = [generate_market() for _ in range(N_MARKETS)]
    print('Done. Running both variants...\n')

    baseline_results = []
    improved_results = []

    for i, (up_p, dn_p, outcome) in enumerate(markets):
        if i % 2000 == 0 and i > 0:
            print(f'  ... {i:,} / {N_MARKETS:,}', flush=True)
        baseline_results.append(run_baseline(up_p, dn_p, outcome))
        improved_results.append(run_improved(up_p, dn_p, outcome))

    sep = '═' * 62
    print(f'\n{sep}')
    print('  LADDERMATE A/B — MONTE CARLO  (10 000 markets, same paths)')
    print(sep)

    print_stats('BASELINE  (current LadderMateStrategy logic)', baseline_results)
    print()
    print_stats('IMPROVED  (entry confirm + sell-on-flip + pyramid + reentry)', improved_results)

    # ── Delta summary ─────────────────────────────────────────────────────
    b_pnls = [r['pnl'] for r in baseline_results if r['traded']]
    i_pnls = [r['pnl'] for r in improved_results if r['traded']]
    b_wins = sum(1 for p in b_pnls if p > 0)
    i_wins = sum(1 for p in i_pnls if p > 0)

    print(f'\n{sep}')
    print('  DELTA  (Improved − Baseline)')
    print(sep)
    delta_ev   = (sum(i_pnls)/len(i_pnls) if i_pnls else 0) - (sum(b_pnls)/len(b_pnls) if b_pnls else 0)
    delta_wr   = (i_wins/len(i_pnls)*100 if i_pnls else 0) - (b_wins/len(b_pnls)*100 if b_pnls else 0)
    delta_tot  = sum(i_pnls) - sum(b_pnls)
    print(f'  EV/market  : {delta_ev:>+8.4f} USD')
    print(f'  Winrate    : {delta_wr:>+8.2f} pp')
    print(f'  Total PnL  : {delta_tot:>+8.2f} USD  (over {N_MARKETS:,} markets)')
    print(sep)


if __name__ == '__main__':
    main()
