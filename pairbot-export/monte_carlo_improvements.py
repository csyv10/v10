"""
monte_carlo_improvements.py
════════════════════════════════════════════════════════════════════════════
Monte Carlo: 10 000 markets — A/B test of proposed improvements.

BASELINE: current live config (RUNG_SPREAD=0.050, RUNG_STEP=0.025)

Variants tested (each in isolation, then all combined):
  A  RUNG_STEP 0.025 → 0.040
         Wider gap between rungs — fewer rungs per market, less concentration
  B  Trend confirmation
         Only place a new rung if 3 of last 5 ticks are upward on active side
  C  No new buys after flip-recovery
         Once recovery position is sold, close all new activity for the market
  D  Progressive RUNG_USD
         Rung sizes: $2.00, $1.80, $1.60 ... floor $0.80
         Higher rungs (higher price = higher reversal risk) get smaller size
  E  COMBINED: A + B + C + D together
"""

import random
import statistics
from typing import List, Tuple, Optional

random.seed(42)

# ─── Market simulation parameters ────────────────────────────────────────────
N_MARKETS      = 10_000
N_TICKS        = 300
FLIP_PROB      = 0.35
FLIP_WINDOW    = (60, 220)
NOISE_SIGMA    = 0.008
DRIFT_STRENGTH = 0.003
OUTCOME_PRICE  = 0.85
LOSER_PRICE    = 0.15
OPEN_SPREAD    = 0.04

# ─── Baseline strategy parameters ─────────────────────────────────────────────
ENTRY_MIN        = 0.28
ENTRY_MAX        = 0.60
RUNG_SPREAD      = 0.050   # already updated in laddermate_strategy.py
RUNG_STEP_BASE   = 0.025   # baseline
RUNG_USD_BASE    = 2.00
MAX_RUNG_PRICE   = 0.92
MAX_RUNGS        = 10
FLIP_TRIGGER     = 0.50
FLIP_CONFIRM     = 3
RECOVERY_BUDGET  = 3.00
RECOVERY_TARGET  = 2.00
MAX_SIDE_COST    = 12.0
LOSS_CAP         = 3.00
MIN_TRADE        = 1.00
EXIT_TTC         = 50
EXIT_MIN_BID     = 0.28
MIN_RR           = 1.5

# ─── Improvement parameters ───────────────────────────────────────────────────
RUNG_STEP_NEW        = 0.040   # A: wider step
TREND_CONFIRM_WINDOW = 5       # B: look-back window for trend confirmation
TREND_CONFIRM_MIN    = 3       # B: min up-ticks required out of window
PROG_RUNG_STEP       = 0.20    # D: reduce RUNG_USD by this per rung placed
PROG_RUNG_FLOOR      = 0.80    # D: minimum rung size


# ═══════════════════════════════════════════════════════════════════════════════
# Price path generator
# ═══════════════════════════════════════════════════════════════════════════════

def generate_market() -> Tuple[List[float], List[float], str]:
    does_flip = random.random() < FLIP_PROB
    if does_flip:
        flip_tick = random.randint(*FLIP_WINDOW)
        first_winner  = 'UP'   if random.random() < 0.5 else 'DOWN'
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
# Position tracker
# ═══════════════════════════════════════════════════════════════════════════════

class _Pos:
    def __init__(self):
        self.qty = 0.0; self.cost = 0.0

    @property
    def avg(self):
        return self.cost / self.qty if self.qty > 0.001 else 0.0

    def add(self, qty, price):
        self.cost += qty * price; self.qty += qty

    def remove(self, qty) -> float:
        qty = min(qty, self.qty)
        if self.qty < 0.001: return 0.0
        cb = self.avg * qty
        self.qty -= qty; self.cost -= cb
        if self.qty < 0.01: self.qty = 0.0; self.cost = 0.0
        return cb

    def clear(self):
        self.qty = 0.0; self.cost = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Simulate one market
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_market(
    up_prices, dn_prices, outcome,
    use_wider_step:   bool = False,   # A
    use_trend_conf:   bool = False,   # B
    no_buy_post_rec:  bool = False,   # C
    use_prog_sizing:  bool = False,   # D
) -> float:

    rung_step = RUNG_STEP_NEW if use_wider_step else RUNG_STEP_BASE

    pos      = {'UP': _Pos(), 'DOWN': _Pos()}
    rungs    = []
    realised = 0.0
    cash_out = 0.0

    phase         = 'scout'
    ladder_side   = None
    flip_ticks    = 0
    flipped_once  = False
    last_rung_px  = 0.0
    recovery_done = False   # C: set True after recovery sell

    rec_side   = None
    rec_qty    = 0.0
    rec_target = 0.0

    rung_count    = 0          # D: how many rungs placed total (for progressive sizing)
    price_hist    = {'UP': [], 'DOWN': []}

    sl_dist = round(RUNG_SPREAD / MIN_RR, 4)

    def available():
        spent = pos['UP'].cost + pos['DOWN'].cost
        return max(0.0, min(1000.0 - spent, 250.0 - spent))

    def loss_room(side):
        other_outcome = 'DOWN' if side == 'UP' else 'UP'
        pnl_other = (pos[other_outcome].qty
                     - (pos['UP'].cost + pos['DOWN'].cost)
                     + realised)
        return max(0.0, pnl_other + LOSS_CAP)

    def rung_usd():
        # D: progressive sizing — reduce by PROG_RUNG_STEP per rung placed
        if not use_prog_sizing:
            return RUNG_USD_BASE
        return max(PROG_RUNG_FLOOR, RUNG_USD_BASE - rung_count * PROG_RUNG_STEP)

    def do_buy(side, price, usd):
        nonlocal cash_out
        usd = min(usd, loss_room(side), available())
        if usd < MIN_TRADE or price <= 0.01: return None
        qty = usd / price
        cash_out     += usd
        pos[side].add(qty, price)
        return qty

    def do_sell(side, price, qty):
        nonlocal realised
        qty = min(qty, pos[side].qty)
        if qty < 0.001: return
        cb  = pos[side].remove(qty)
        prc = qty * price
        realised += prc - cb

    def open_rung_count(side):
        return sum(1 for r in rungs if r['side'] == side)

    def open_rung_cost(side):
        return sum(r['cost'] for r in rungs if r['side'] == side)

    def trend_ok(side) -> bool:
        # B: require TREND_CONFIRM_MIN up-ticks out of last TREND_CONFIRM_WINDOW
        if not use_trend_conf: return True
        hist = price_hist[side]
        if len(hist) < TREND_CONFIRM_WINDOW + 1: return True  # not enough data → allow
        recent = hist[-(TREND_CONFIRM_WINDOW + 1):]
        up_ticks = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
        return up_ticks >= TREND_CONFIRM_MIN

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        bid_up  = max(0.01, up - 0.005)
        bid_dn  = max(0.01, dn - 0.005)
        bid     = {'UP': bid_up, 'DOWN': bid_dn}
        px      = {'UP': up, 'DOWN': dn}
        leading = 'UP' if up >= dn else 'DOWN'

        price_hist['UP'].append(up)
        price_hist['DOWN'].append(dn)

        # ── EXIT WINDOW ──────────────────────────────────────────────────────
        if ttc < EXIT_TTC:
            sold = []
            for r in rungs:
                if r.get('hold_to_res'): continue
                s = r['side']
                if bid[s] >= EXIT_MIN_BID:
                    do_sell(s, bid[s], r['qty'])
                    sold.append(r)
            for r in sold: rungs.remove(r)

        if ttc < 10:
            continue

        # ── SELL CHECK: TP + SL ──────────────────────────────────────────────
        sold = []
        for r in rungs:
            if r.get('hold_to_res'): continue
            s  = r['side']; cp = px[s]
            if cp >= r['sell_target'] or cp <= r['stop_price']:
                do_sell(s, cp, r['qty']); sold.append(r)
        for r in sold: rungs.remove(r)

        # ── RECOVERY SELL ────────────────────────────────────────────────────
        if rec_side and rec_qty > 0.01:
            if px[rec_side] >= rec_target:
                do_sell(rec_side, px[rec_side], pos[rec_side].qty)
                rec_qty  = 0.0
                rec_side = None
                recovery_done = True   # C: mark recovery as done
                if ladder_side and not (no_buy_post_rec):
                    phase = 'ladder'
                # C: if no_buy_post_rec, stay in flip_recover so no new rungs

        # ── SCOUT ────────────────────────────────────────────────────────────
        if phase == 'scout':
            lp = px[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                usd = rung_usd()
                qty = do_buy(leading, lp, usd)
                if qty:
                    rung_count += 1
                    rungs.append({
                        'side': leading, 'buy_price': lp,
                        'sell_target': round(lp + RUNG_SPREAD, 4),
                        'stop_price':  round(lp - sl_dist, 4),
                        'qty': qty, 'cost': qty * lp, 'hold_to_res': False,
                    })
                    ladder_side  = leading
                    last_rung_px = lp
                    phase        = 'ladder'
            continue

        # ── LADDER ───────────────────────────────────────────────────────────
        if phase == 'ladder':
            ls = ladder_side; ls_price = px[ls]
            other = 'DOWN' if ls == 'UP' else 'UP'

            # flip detection
            if ls_price < FLIP_TRIGGER and leading != ls:
                flip_ticks += 1
            else:
                flip_ticks  = 0

            if flip_ticks >= FLIP_CONFIRM and not flipped_once:
                flipped_once = True
                other_price  = px[other]
                for r in rungs:
                    if r['side'] == ls: r['hold_to_res'] = True
                open_cost  = open_rung_cost(ls)
                net_needed = open_cost + RECOVERY_TARGET
                rec_budget = min(RECOVERY_BUDGET, available(),
                                 MAX_SIDE_COST - pos[other].cost)
                if rec_budget >= MIN_TRADE and other_price > 0.01:
                    qty = do_buy(other, other_price, rec_budget)
                    if qty:
                        sell_tgt   = min(other_price + net_needed / qty, MAX_RUNG_PRICE)
                        rec_side   = other; rec_qty = qty; rec_target = sell_tgt
                        phase      = 'flip_recover'
                        ladder_side = other; last_rung_px = other_price

            # add new rungs (with optional trend confirmation)
            if phase == 'ladder':
                oc = open_rung_count(ls)
                usd = rung_usd()
                if (ls_price >= last_rung_px + rung_step
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS
                        and available() >= usd
                        and pos[ls].cost + usd <= MAX_SIDE_COST
                        and trend_ok(ls)):           # B: trend gate
                    qty = do_buy(ls, ls_price, usd)
                    if qty:
                        rung_count += 1
                        target = round(ls_price + RUNG_SPREAD, 4)
                        stop_p = round(ls_price - sl_dist, 4)
                        rungs.append({
                            'side': ls, 'buy_price': ls_price,
                            'sell_target': target, 'stop_price': stop_p,
                            'qty': qty, 'cost': qty * ls_price, 'hold_to_res': False,
                        })
                        last_rung_px = ls_price

        elif phase == 'flip_recover':
            # C: if recovery done + no_buy_post_rec → don't add rungs
            if recovery_done and no_buy_post_rec:
                continue
            ls = ladder_side; ls_price = px[ls]
            oc = open_rung_count(ls)
            usd = rung_usd()
            if (ls_price >= last_rung_px + rung_step
                    and ls_price <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS
                    and available() >= usd
                    and pos[ls].cost + usd <= MAX_SIDE_COST
                    and trend_ok(ls)):
                qty = do_buy(ls, ls_price, usd)
                if qty:
                    rung_count += 1
                    target = round(ls_price + RUNG_SPREAD, 4)
                    stop_p = round(ls_price - sl_dist, 4)
                    rungs.append({
                        'side': ls, 'buy_price': ls_price,
                        'sell_target': target, 'stop_price': stop_p,
                        'qty': qty, 'cost': qty * ls_price, 'hold_to_res': False,
                    })
                    last_rung_px = ls_price

    # ── RESOLUTION ──────────────────────────────────────────────────────────
    payout = pos[outcome].qty
    cost   = pos['UP'].cost + pos['DOWN'].cost
    realised += payout - cost
    return realised


# ═══════════════════════════════════════════════════════════════════════════════
# Run
# ═══════════════════════════════════════════════════════════════════════════════

VARIANTS = [
    dict(label='BASELINE  (current live)',          a=False, b=False, c=False, d=False),
    dict(label='A: wider RUNG_STEP (0.040)',         a=True,  b=False, c=False, d=False),
    dict(label='B: trend confirmation (3/5 ticks)',  a=False, b=True,  c=False, d=False),
    dict(label='C: no buys after flip-recovery',     a=False, b=False, c=True,  d=False),
    dict(label='D: progressive rung sizing',         a=False, b=False, c=False, d=True),
    dict(label='E: COMBINED (A+B+C+D)',              a=True,  b=True,  c=True,  d=True),
]


def run():
    print(f'Generating {N_MARKETS:,} market paths...')
    markets = [generate_market() for _ in range(N_MARKETS)]

    results = []
    for v in VARIANTS:
        pnls = [
            simulate_market(up, dn, out,
                            use_wider_step=v['a'],
                            use_trend_conf=v['b'],
                            no_buy_post_rec=v['c'],
                            use_prog_sizing=v['d'])
            for up, dn, out in markets
        ]
        wins     = sum(1 for p in pnls if p > 0)
        losses   = sum(1 for p in pnls if p < 0)
        total    = sum(pnls)
        avg      = statistics.mean(pnls)
        std      = statistics.stdev(pnls)
        wr       = wins / len(pnls) * 100
        avg_win  = statistics.mean(p for p in pnls if p > 0) if wins  else 0
        avg_loss = statistics.mean(p for p in pnls if p < 0) if losses else 0
        rr       = abs(avg_win / avg_loss) if avg_loss else float('inf')
        sorted_p = sorted(pnls)
        p5       = sorted_p[int(0.05 * len(sorted_p))]
        p95      = sorted_p[int(0.95 * len(sorted_p))]
        worst    = min(pnls)
        results.append(dict(label=v['label'], total=total, avg=avg, std=std,
                            wr=wr, rr=rr, avg_win=avg_win, avg_loss=avg_loss,
                            p5=p5, p95=p95, worst=worst, wins=wins, losses=losses))
        print(f'  ✓ {v["label"]:<42}  avg=${avg:+.4f}  WR={wr:.1f}%  R:R={rr:.2f}x')

    baseline = results[0]

    print(f"\n{'═'*80}")
    print(f"  RESULTS vs BASELINE  —  {N_MARKETS:,} markets  |  RUNG_SPREAD={RUNG_SPREAD}")
    print(f"{'═'*80}")
    hdr = (f"  {'Variant':<42}  {'Avg$/mkt':>9}  {'Δ vs base':>10}  "
           f"{'WR%':>5}  {'R:R':>5}  {'Std':>6}  {'P5':>7}  {'P95':>7}")
    print(hdr)
    print(f"  {'-'*42}  {'-'*9}  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*7}")

    for r in results:
        delta = r['avg'] - baseline['avg']
        d_str = f'{delta:+.4f}' if r['label'] != baseline['label'] else '  —     '
        arrow = ' ✅' if delta > 0.005 else (' ❌' if delta < -0.005 else ' ➖')
        tag   = arrow if r['label'] != baseline['label'] else ''
        print(f"  {r['label']:<42}  ${r['avg']:>+8.4f}  {d_str:>10}  "
              f"{r['wr']:>5.1f}%  {r['rr']:>5.2f}x  "
              f"${r['std']:>5.4f}  ${r['p5']:>+6.4f}  ${r['p95']:>+6.4f}{tag}")

    # find best
    best = max(results[1:], key=lambda r: r['avg'])
    delta_best = best['avg'] - baseline['avg']
    delta_tot  = best['total'] - baseline['total']
    print(f"\n{'═'*80}")
    print(f"  🏆 BEST VARIANT:  {best['label']}")
    print(f"     vs baseline:   avg ${delta_best:+.4f}/market  "
          f"(${delta_tot:+,.2f} over {N_MARKETS:,} markets)")
    print(f"{'═'*80}\n")

    # ASCII bar chart: avg PnL per variant
    print("  Avg PnL / market (bar chart):\n")
    max_avg = max(abs(r['avg']) for r in results)
    bar_width = 35
    for r in results:
        filled = int(abs(r['avg']) / max_avg * bar_width)
        bar    = ('█' if r['avg'] >= 0 else '▒') * filled
        print(f"  {r['label']:<42}  ${r['avg']:>+7.4f}  {bar}")


if __name__ == '__main__':
    print(f'\nMonte Carlo: {N_MARKETS:,} markets — improvement variants')
    print(f'RUNG_SPREAD={RUNG_SPREAD}  LOSS_CAP={LOSS_CAP}\n')
    run()
