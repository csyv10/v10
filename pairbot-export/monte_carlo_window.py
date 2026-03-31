"""
monte_carlo_window.py
════════════════════════════════════════════════════════════════════════════
MC: 10 000 markets — VOL_DETECT_WINDOW sensitivity test

Tests windows: 5, 10, 15, 20, 25, 30 ticks (30 = current live config).

For each window reports:
  - Avg PnL / market
  - Win rate & R:R
  - Classification accuracy vs ground-truth (30-tick label)
  - False-positive rate (calm labelled as volatile)
  - False-negative rate (volatile labelled as calm)
  - Ticks "wasted" observing vs ticks available to trade

Ground truth: volatility label from the full 30-tick window.
"""

import random
import statistics
from typing import List, Tuple, Optional

random.seed(42)

# ─── Market parameters ────────────────────────────────────────────────────────
N_MARKETS      = 10_000
N_TICKS        = 300
FLIP_PROB      = 0.35
FLIP_WINDOW    = (60, 220)
NOISE_SIGMA    = 0.008
DRIFT_STRENGTH = 0.003
OUTCOME_PRICE  = 0.85
LOSER_PRICE    = 0.15
OPEN_SPREAD    = 0.04

# ─── Strategy parameters ──────────────────────────────────────────────────────
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
EXIT_TTC        = 50
EXIT_MIN_BID    = 0.28
MIN_RR          = 1.5
FLIP_BUDGET     = 10.00

# ─── Volatility regime parameters ─────────────────────────────────────────────
VOL_THRESHOLD      = 0.06
VOL_MAX_RUNGS      = 4
VOL_SL_MULT        = 1.5
VOL_MOMENTUM_TICKS = 3
VOL_EXIT_EXTRA     = 30

# ─── Windows to test ─────────────────────────────────────────────────────────
WINDOWS = [5, 10, 15, 20, 25, 30]
GROUND_TRUTH_WINDOW = 30   # reference for classification accuracy


# ═══════════════════════════════════════════════════════════════════════════════
# Price path generator
# ═══════════════════════════════════════════════════════════════════════════════

def generate_market() -> Tuple[List[float], List[float], str]:
    does_flip   = random.random() < FLIP_PROB
    flip_tick   = random.randint(*FLIP_WINDOW) if does_flip else N_TICKS + 1
    first_w     = 'UP' if random.random() < 0.5 else 'DOWN'
    second_w    = 'DOWN' if first_w == 'UP' else 'UP'
    final_out   = second_w if does_flip else first_w

    up0 = 0.50 + (OPEN_SPREAD/2 if first_w == 'UP' else -OPEN_SPREAD/2) + random.gauss(0, 0.01)
    dn0 = 0.50 + (OPEN_SPREAD/2 if first_w == 'DOWN' else -OPEN_SPREAD/2) + random.gauss(0, 0.01)
    up_prices = [max(0.05, min(0.95, up0))]
    dn_prices = [max(0.05, min(0.95, dn0))]

    for t in range(1, N_TICKS):
        winner = first_w if t < flip_tick else second_w
        prog   = t / N_TICKS
        ds     = DRIFT_STRENGTH * (0.5 + prog)
        up_tgt = OUTCOME_PRICE if winner == 'UP' else LOSER_PRICE
        dn_tgt = OUTCOME_PRICE if winner == 'DOWN' else LOSER_PRICE
        up_new = up_prices[-1] + ds*(up_tgt - up_prices[-1]) + random.gauss(0, NOISE_SIGMA)
        dn_new = dn_prices[-1] + ds*(dn_tgt - dn_prices[-1]) + random.gauss(0, NOISE_SIGMA)
        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.15:
            c = (mid - 0.5) * 0.3; up_new -= c; dn_new -= c
        up_prices.append(max(0.02, min(0.98, up_new)))
        dn_prices.append(max(0.02, min(0.98, dn_new)))

    return up_prices, dn_prices, final_out


def vol_label(up_prices, dn_prices, window: int) -> bool:
    """Classify volatility using the leading side over the first `window` ticks."""
    leading_w = up_prices[:window] if up_prices[0] >= dn_prices[0] else dn_prices[:window]
    return (max(leading_w) - min(leading_w)) > VOL_THRESHOLD


# ═══════════════════════════════════════════════════════════════════════════════
# Position tracker
# ═══════════════════════════════════════════════════════════════════════════════

class _Pos:
    def __init__(self): self.qty = 0.0; self.cost = 0.0
    @property
    def avg(self): return self.cost / self.qty if self.qty > 0.001 else 0.0
    def add(self, qty, price): self.cost += qty*price; self.qty += qty
    def remove(self, qty):
        qty = min(qty, self.qty)
        if self.qty < 0.001: return 0.0
        cb = self.avg*qty; self.qty -= qty; self.cost -= cb
        if self.qty < 0.01: self.qty = 0.0; self.cost = 0.0
        return cb


# ═══════════════════════════════════════════════════════════════════════════════
# Simulate one market with a given detection window
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_market(up_prices, dn_prices, outcome, detect_window: int) -> float:
    pos      = {'UP': _Pos(), 'DOWN': _Pos()}
    rungs: List[dict] = []
    realised = 0.0
    phase        = 'observe'   # observe → scout → ladder → flip_recover
    ladder_side  = None
    flip_ticks   = 0
    flip_budget  = FLIP_BUDGET
    last_rung_px = 0.0
    rec_side:    Optional[str] = None
    rec_qty:     float         = 0.0
    rec_target:  float         = 0.0
    is_volatile: bool          = False
    vol_prices:  List[float]   = []
    momentum:    int           = 0
    prev_ls_px:  float         = 0.0

    def available():
        spent = pos['UP'].cost + pos['DOWN'].cost
        return max(0.0, min(1000.0 - spent, 250.0 - spent))

    def loss_room(side):
        other = 'DOWN' if side == 'UP' else 'UP'
        return max(0.0, pos[other].qty - (pos['UP'].cost + pos['DOWN'].cost) + realised + LOSS_CAP)

    def do_buy(side, price, usd):
        usd = min(usd, loss_room(side), available())
        if usd < MIN_TRADE or price <= 0.01: return None
        qty = usd / price; pos[side].add(qty, price); return qty

    def do_sell(side, price, qty):
        nonlocal realised
        qty = min(qty, pos[side].qty)
        if qty < 0.001: return
        cb = pos[side].remove(qty); realised += qty*price - cb

    def open_count(side): return sum(1 for r in rungs if r['side'] == side)
    def open_cost(side):  return sum(r['cost'] for r in rungs if r['side'] == side)

    def sl_dist():
        return round((RUNG_SPREAD / MIN_RR) * (VOL_SL_MULT if is_volatile else 1.0), 4)

    def max_rungs_eff():
        return VOL_MAX_RUNGS if is_volatile else MAX_RUNGS

    def exit_ttc_eff():
        return EXIT_TTC + (VOL_EXIT_EXTRA if is_volatile else 0)

    def momentum_ok(ls_price) -> bool:
        nonlocal momentum, prev_ls_px
        if not is_volatile: return True
        if ls_price > prev_ls_px > 0:
            momentum = min(momentum + 1, VOL_MOMENTUM_TICKS)
        else:
            momentum = 0
        prev_ls_px = ls_price
        return momentum >= VOL_MOMENTUM_TICKS

    def try_flip(ls, other, other_price, avail_now) -> float:
        nonlocal flip_budget, rec_side, rec_qty, rec_target, ladder_side, last_rung_px
        nonlocal flip_ticks, phase
        for r in rungs:
            if r['side'] == ls and not r.get('hold_to_res'): r['hold_to_res'] = True
        oc       = open_cost(ls)
        needed   = oc + RECOVERY_TARGET
        budget   = min(RECOVERY_BUDGET, flip_budget, avail_now,
                       MAX_SIDE_COST - pos[other].cost)
        if budget < MIN_TRADE or other_price <= 0.01: return 0.0
        qty = do_buy(other, other_price, budget)
        if not qty: return 0.0
        spent      = qty * other_price
        flip_budget -= spent
        sell_tgt   = min(other_price + needed / qty, MAX_RUNG_PRICE)
        rec_side   = other; rec_qty = qty; rec_target = sell_tgt
        ladder_side = other; last_rung_px = other_price
        phase      = 'flip_recover'
        flip_ticks = 0
        return spent

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        bid     = {'UP': max(0.01, up-0.005), 'DOWN': max(0.01, dn-0.005)}
        px      = {'UP': up, 'DOWN': dn}
        leading = 'UP' if up >= dn else 'DOWN'

        # ── collect vol prices (first detect_window ticks, all phases) ────────
        if t < detect_window:
            lead_px = up if up >= dn else dn
            vol_prices.append(lead_px)
        if t == detect_window - 1:
            is_volatile = (max(vol_prices) - min(vol_prices)) > VOL_THRESHOLD
            if phase == 'observe':
                phase = 'scout'

        # ── exit window ───────────────────────────────────────────────────────
        if ttc < exit_ttc_eff():
            sold = [r for r in rungs
                    if not r.get('hold_to_res') and bid[r['side']] >= EXIT_MIN_BID]
            for r in sold: do_sell(r['side'], bid[r['side']], r['qty']); rungs.remove(r)

        if ttc < 10: continue

        # ── TP / SL ───────────────────────────────────────────────────────────
        sold = []
        for r in rungs:
            if r.get('hold_to_res'): continue
            s = r['side']; cp = px[s]
            if cp >= r['sell_target'] or cp <= r['stop_price']:
                do_sell(s, cp, r['qty']); sold.append(r)
        for r in sold: rungs.remove(r)

        # ── recovery sell ──────────────────────────────────────────────────────
        if rec_side and rec_qty > 0.01 and px[rec_side] >= rec_target:
            do_sell(rec_side, px[rec_side], pos[rec_side].qty)
            rec_qty = 0.0; rec_side = None
            if ladder_side: phase = 'ladder'

        avail = available()

        # ── observe: waiting for detection window to complete ─────────────────
        if phase == 'observe':
            continue

        # ── scout ─────────────────────────────────────────────────────────────
        if phase == 'scout':
            lp = px[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                qty = do_buy(leading, lp, RUNG_USD)
                if qty:
                    sd = sl_dist()
                    rungs.append({'side': leading, 'buy_price': lp,
                                  'sell_target': round(lp+RUNG_SPREAD, 4),
                                  'stop_price':  round(lp-sd, 4),
                                  'qty': qty, 'cost': qty*lp, 'hold_to_res': False})
                    ladder_side = leading; last_rung_px = lp; phase = 'ladder'
            continue

        # ── ladder ────────────────────────────────────────────────────────────
        if phase == 'ladder':
            ls       = ladder_side; ls_price = px[ls]
            other    = 'DOWN' if ls == 'UP' else 'UP'

            if ls_price < FLIP_TRIGGER and leading != ls: flip_ticks += 1
            else:                                          flip_ticks = 0

            if flip_ticks >= FLIP_CONFIRM and flip_budget >= MIN_TRADE:
                try_flip(ls, other, px[other], avail)

            if phase == 'ladder':
                oc = open_count(ls)
                ok = momentum_ok(ls_price)
                if (ls_price >= last_rung_px + RUNG_STEP
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < max_rungs_eff()
                        and avail >= RUNG_USD and ok
                        and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    qty = do_buy(ls, ls_price, RUNG_USD)
                    if qty:
                        sd = sl_dist()
                        rungs.append({'side': ls, 'buy_price': ls_price,
                                      'sell_target': round(ls_price+RUNG_SPREAD, 4),
                                      'stop_price':  round(ls_price-sd, 4),
                                      'qty': qty, 'cost': qty*ls_price, 'hold_to_res': False})
                        last_rung_px = ls_price

        # ── flip_recover ──────────────────────────────────────────────────────
        elif phase == 'flip_recover':
            ls       = ladder_side; ls_price = px[ls]
            other    = 'DOWN' if ls == 'UP' else 'UP'

            if ls_price < FLIP_TRIGGER and leading != ls: flip_ticks += 1
            else:                                          flip_ticks = 0

            if flip_ticks >= FLIP_CONFIRM and flip_budget >= MIN_TRADE:
                try_flip(ls, other, px[other], avail)
                ls       = ladder_side; ls_price = px[ls]

            if phase in ('flip_recover', 'ladder'):
                oc = open_count(ls)
                ok = momentum_ok(ls_price)
                if (ls_price >= last_rung_px + RUNG_STEP
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < max_rungs_eff()
                        and avail >= RUNG_USD and ok
                        and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    qty = do_buy(ls, ls_price, RUNG_USD)
                    if qty:
                        sd = sl_dist()
                        rungs.append({'side': ls, 'buy_price': ls_price,
                                      'sell_target': round(ls_price+RUNG_SPREAD, 4),
                                      'stop_price':  round(ls_price-sd, 4),
                                      'qty': qty, 'cost': qty*ls_price, 'hold_to_res': False})
                        last_rung_px = ls_price

    # ── resolution ─────────────────────────────────────────────────────────────
    payout   = pos[outcome].qty
    realised += payout - (pos['UP'].cost + pos['DOWN'].cost)
    return realised


# ═══════════════════════════════════════════════════════════════════════════════
# Run
# ═══════════════════════════════════════════════════════════════════════════════

def stats(pnls):
    wins     = sum(1 for p in pnls if p > 0)
    losses   = sum(1 for p in pnls if p < 0)
    avg      = statistics.mean(pnls)
    std      = statistics.stdev(pnls)
    wr       = wins / len(pnls) * 100
    avg_win  = statistics.mean(p for p in pnls if p > 0) if wins  else 0
    avg_loss = statistics.mean(p for p in pnls if p < 0) if losses else 0
    rr       = abs(avg_win / avg_loss) if avg_loss else float('inf')
    sp       = sorted(pnls)
    p5       = sp[int(0.05*len(sp))]
    p95      = sp[int(0.95*len(sp))]
    return dict(avg=avg, std=std, wr=wr, rr=rr, p5=p5, p95=p95)


def run():
    print(f'Generating {N_MARKETS:,} markets...')
    markets = [(generate_market()) for _ in range(N_MARKETS)]

    # ground-truth labels (30-tick window)
    gt_labels = [vol_label(up, dn, GROUND_TRUTH_WINDOW) for up, dn, _ in markets]
    n_gt_vol  = sum(gt_labels)
    print(f'Ground-truth volatile (30t): {n_gt_vol:,} ({n_gt_vol/N_MARKETS*100:.1f}%)\n')

    print(f'{"Win":>4}  {"Detect":>6}  {"Ticks":>5}  {"Trade":>5}  {"Avg PnL":>8}  '
          f'{"std":>6}  {"WR%":>5}  {"R:R":>5}  '
          f'{"P5":>7}  {"P95":>7}  '
          f'{"Acc%":>5}  {"FP%":>5}  {"FN%":>5}')
    print('─' * 100)

    results = {}
    for w in WINDOWS:
        pnls = [simulate_market(up, dn, out, w) for up, dn, out in markets]
        s    = stats(pnls)

        # classification accuracy vs ground truth
        w_labels = [vol_label(up, dn, w) for up, dn, _ in markets]
        correct  = sum(a == b for a, b in zip(w_labels, gt_labels))
        fp       = sum(1 for a, b in zip(w_labels, gt_labels) if a and not b)   # called vol, was calm
        fn       = sum(1 for a, b in zip(w_labels, gt_labels) if not a and b)   # called calm, was vol
        acc      = correct / N_MARKETS * 100
        fp_pct   = fp / (N_MARKETS - n_gt_vol) * 100 if N_MARKETS - n_gt_vol else 0
        fn_pct   = fn / n_gt_vol * 100 if n_gt_vol else 0

        trade_ticks = N_TICKS - w

        marker = ' ◀ CURRENT' if w == GROUND_TRUTH_WINDOW else ''
        print(f'  {w:>4}t  {w:>5}t  {trade_ticks:>5}  '
              f'${s["avg"]:>+7.4f}  {s["std"]:>6.4f}  '
              f'{s["wr"]:>5.1f}%  {s["rr"]:>5.2f}x  '
              f'${s["p5"]:>+6.3f}  ${s["p95"]:>+6.3f}  '
              f'{acc:>5.1f}%  {fp_pct:>5.1f}%  {fn_pct:>5.1f}%'
              f'{marker}')
        results[w] = {'pnl': s, 'acc': acc, 'fp': fp_pct, 'fn': fn_pct}

    # ── delta vs current (30t) ─────────────────────────────────────────────────
    base_avg = results[GROUND_TRUTH_WINDOW]['pnl']['avg']
    print(f'\n  Δ avg PnL vs 30-tick window:')
    for w in WINDOWS:
        d = results[w]['pnl']['avg'] - base_avg
        bar = ('▲' if d >= 0 else '▼') + '█' * int(abs(d) / 0.01)
        print(f'    {w:>2}t: {d:>+7.4f}/mkt  ${d*N_MARKETS:>+8.2f} total  '
              f'acc={results[w]["acc"]:.1f}%  fp={results[w]["fp"]:.1f}%  fn={results[w]["fn"]:.1f}%  {bar}')

    # ── recommendation ─────────────────────────────────────────────────────────
    best_w    = max(WINDOWS, key=lambda w: results[w]['pnl']['avg'])
    best_pnl  = results[best_w]['pnl']['avg']
    print(f'\n  Best window: {best_w}t  (avg=${best_pnl:+.4f})')
    print(f'  Current:     30t  (avg=${base_avg:+.4f})')
    print()


if __name__ == '__main__':
    print(f'\nMonte Carlo: VOL_DETECT_WINDOW sensitivity — {N_MARKETS:,} markets')
    print(f'VOL_THRESHOLD={VOL_THRESHOLD}  VOL_MAX_RUNGS={VOL_MAX_RUNGS}  '
          f'VOL_SL_MULT={VOL_SL_MULT}  FLIP_BUDGET={FLIP_BUDGET}\n')
    run()
