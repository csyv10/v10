"""
monte_carlo_multiflip.py
════════════════════════════════════════════════════════════════════════════
Monte Carlo: 10 000 markets — multi-flip strategies

Tests 4 variants vs a shared baseline:

  BASELINE     : current logic — old rungs → hold_to_res, only 1 flip response
  ACTIVE_SELL  : at flip time, sell old-side rungs at bid if bid >= EXIT_MIN_BID,
                 only mark hold_to_res if bid too low (frees capital for recovery)
  MULTI_FLIP   : flip budget pool (FLIP_BUDGET total $). No _flipped_once lock.
                 Bot responds to every new flip until budget is exhausted.
  COMBINED     : active sell at each flip + multi-flip budget pool

Market generation includes realistic multi-flip paths (0/1/2/3 flips).
"""

import random
import statistics
from typing import List, Tuple, Optional

random.seed(42)

# ─── Market simulation parameters ────────────────────────────────────────────
N_MARKETS      = 10_000
N_TICKS        = 300
NOISE_SIGMA    = 0.008
DRIFT_STRENGTH = 0.003
OUTCOME_PRICE  = 0.85
LOSER_PRICE    = 0.15
OPEN_SPREAD    = 0.04

# Multi-flip probabilities: P(0)=0.50, P(1)=0.23, P(2)=0.13, P(3)=0.07, P(4)=0.04, P(5)=0.03
FLIP_PROBS     = [0.50, 0.23, 0.13, 0.07, 0.04, 0.03]
MIN_FLIP_SPACE = 50    # minimum ticks between flips
FLIP_EARLIEST  = 50    # earliest tick a flip can occur
FLIP_LATEST    = 260   # latest tick a flip can start

# ─── Strategy parameters (matching live config) ───────────────────────────────
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

# ─── Multi-flip variant parameters ───────────────────────────────────────────
FLIP_BUDGET      = 6.00   # total $ available across all flip recoveries in a market
FLIP_COOLDOWN    = 20     # ticks after a flip where further flip-detection is suppressed


# ═══════════════════════════════════════════════════════════════════════════════
# Market generator — supports 0, 1, 2, or 3 flips
# ═══════════════════════════════════════════════════════════════════════════════

def generate_market() -> Tuple[List[float], List[float], str, int]:
    """Returns (up_prices, dn_prices, final_outcome, n_flips)."""

    # ── draw number of flips ─────────────────────────────────────────────────
    r = random.random()
    cum = 0.0
    n_flips = 0
    for i, p in enumerate(FLIP_PROBS):
        cum += p
        if r < cum:
            n_flips = i
            break

    # ── place flip ticks ─────────────────────────────────────────────────────
    flip_ticks: List[int] = []
    earliest = FLIP_EARLIEST
    for _ in range(n_flips):
        latest = min(FLIP_LATEST, N_TICKS - MIN_FLIP_SPACE * (n_flips - len(flip_ticks)))
        if earliest >= latest:
            break
        t = random.randint(earliest, latest)
        flip_ticks.append(t)
        earliest = t + MIN_FLIP_SPACE

    n_flips = len(flip_ticks)  # may have been trimmed

    # ── determine winner sequence ─────────────────────────────────────────────
    first_winner = 'UP' if random.random() < 0.5 else 'DOWN'
    sides = [first_winner]
    for _ in flip_ticks:
        sides.append('DOWN' if sides[-1] == 'UP' else 'UP')
    final_outcome = sides[-1]

    # ── generate prices ──────────────────────────────────────────────────────
    if first_winner == 'UP':
        up0 = 0.50 + OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 - OPEN_SPREAD / 2 + random.gauss(0, 0.01)
    else:
        up0 = 0.50 - OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 + OPEN_SPREAD / 2 + random.gauss(0, 0.01)

    up_prices = [max(0.05, min(0.95, up0))]
    dn_prices = [max(0.05, min(0.95, dn0))]

    flip_idx = 0  # which segment we're in
    for t in range(1, N_TICKS):
        if flip_idx < len(flip_ticks) and t >= flip_ticks[flip_idx]:
            flip_idx += 1
        winner_now = sides[flip_idx]
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

    return up_prices, dn_prices, final_outcome, n_flips


# ═══════════════════════════════════════════════════════════════════════════════
# Position tracker
# ═══════════════════════════════════════════════════════════════════════════════

class _Pos:
    def __init__(self):
        self.qty = 0.0; self.cost = 0.0

    @property
    def avg(self): return self.cost / self.qty if self.qty > 0.001 else 0.0

    def add(self, qty, price):
        self.cost += qty * price; self.qty += qty

    def remove(self, qty) -> float:
        qty = min(qty, self.qty)
        if self.qty < 0.001: return 0.0
        cb = self.avg * qty
        self.qty -= qty; self.cost -= cb
        if self.qty < 0.01: self.qty = 0.0; self.cost = 0.0
        return cb


# ═══════════════════════════════════════════════════════════════════════════════
# Simulator
# ═══════════════════════════════════════════════════════════════════════════════

Mode = str  # 'baseline' | 'active_sell' | 'multi_flip' | 'combined' | 'multi_flip_cd' | 'combined_cd'

def simulate_market(up_prices, dn_prices, outcome, mode: Mode) -> float:
    active_sell  = mode in ('active_sell', 'combined', 'combined_cd')
    multi_flip   = mode in ('multi_flip',  'combined', 'multi_flip_cd', 'combined_cd')
    use_cooldown = mode in ('multi_flip_cd', 'combined_cd')

    sl_dist = round(RUNG_SPREAD / MIN_RR, 4)

    pos      = {'UP': _Pos(), 'DOWN': _Pos()}
    rungs: List[dict] = []
    realised = 0.0
    cash_out = 0.0

    phase             = 'scout'
    ladder_side       = None
    flip_ticks        = 0
    flipped_once      = False           # used by BASELINE / ACTIVE_SELL
    flip_budget       = FLIP_BUDGET     # used by MULTI_FLIP / COMBINED
    flip_cd_remaining = 0               # cooldown ticks left after last flip
    last_rung_px      = 0.0

    rec_side:   Optional[str] = None
    rec_qty:    float         = 0.0
    rec_target: float         = 0.0

    def available():
        spent = pos['UP'].cost + pos['DOWN'].cost
        return max(0.0, min(1000.0 - spent, 250.0 - spent))

    def loss_room(side):
        other_outcome = 'DOWN' if side == 'UP' else 'UP'
        pnl_other = (pos[other_outcome].qty
                     - (pos['UP'].cost + pos['DOWN'].cost)
                     + realised)
        return max(0.0, pnl_other + LOSS_CAP)

    def do_buy(side, price, usd):
        nonlocal cash_out
        usd = min(usd, loss_room(side), available())
        if usd < MIN_TRADE or price <= 0.01: return None
        qty = usd / price; cash_out += usd
        pos[side].add(qty, price)
        return qty

    def do_sell(side, price, qty):
        nonlocal realised
        qty = min(qty, pos[side].qty)
        if qty < 0.001: return 0.0
        cb = pos[side].remove(qty)
        pnl = qty * price - cb
        realised += pnl
        return pnl

    def open_rung_count(side):
        return sum(1 for r in rungs if r['side'] == side)

    def open_rung_cost(side):
        return sum(r['cost'] for r in rungs if r['side'] == side)

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        nonlocal_phase = [phase]  # workaround for closure mutation in nested funcs
        ttc     = N_TICKS - t
        bid     = {'UP': max(0.01, up - 0.005), 'DOWN': max(0.01, dn - 0.005)}
        px      = {'UP': up, 'DOWN': dn}
        leading = 'UP' if up >= dn else 'DOWN'

        # ── EXIT WINDOW ──────────────────────────────────────────────────────
        if ttc < EXIT_TTC:
            sold = []
            for r in rungs:
                if r.get('hold_to_res'): continue
                s = r['side']
                if bid[s] >= EXIT_MIN_BID:
                    do_sell(s, bid[s], r['qty']); sold.append(r)
            for r in sold: rungs.remove(r)

        if ttc < 10:
            continue

        # ── SELL CHECK: TP + SL ──────────────────────────────────────────────
        sold = []
        for r in rungs:
            if r.get('hold_to_res'): continue
            s = r['side']; cp = px[s]
            if cp >= r['sell_target'] or cp <= r['stop_price']:
                do_sell(s, cp, r['qty']); sold.append(r)
        for r in sold: rungs.remove(r)

        # ── RECOVERY SELL ────────────────────────────────────────────────────
        if rec_side and rec_qty > 0.01 and px[rec_side] >= rec_target:
            do_sell(rec_side, px[rec_side], pos[rec_side].qty)
            rec_qty = 0.0; rec_side = None
            if ladder_side: phase = 'ladder'

        # ── SCOUT ────────────────────────────────────────────────────────────
        if phase == 'scout':
            lp = px[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                qty = do_buy(leading, lp, RUNG_USD)
                if qty:
                    rungs.append({
                        'side': leading, 'buy_price': lp,
                        'sell_target': round(lp + RUNG_SPREAD, 4),
                        'stop_price':  round(lp - sl_dist, 4),
                        'qty': qty, 'cost': qty * lp, 'hold_to_res': False,
                    })
                    ladder_side = leading; last_rung_px = lp; phase = 'ladder'
            continue

        # ── decrement cooldown ────────────────────────────────────────────────
        if flip_cd_remaining > 0:
            flip_cd_remaining -= 1

        # ─────────────────────────────────────────────────────────────────────
        # FLIP DETECTION helper — shared by ladder and flip_recover phases
        # Returns True if a flip was just executed (transitions phase etc.)
        # ─────────────────────────────────────────────────────────────────────
        def try_flip(ls: str) -> bool:
            nonlocal flip_ticks, flipped_once, flip_budget, flip_cd_remaining
            nonlocal phase, ladder_side, last_rung_px, rec_side, rec_qty, rec_target

            other = 'DOWN' if ls == 'UP' else 'UP'
            other_price = px[other]

            # ── decide whether to respond ─────────────────────────────────
            if multi_flip:
                can_flip = flip_budget >= MIN_TRADE
            else:
                can_flip = not flipped_once

            if not can_flip:
                return False

            # ── mark / sell old-side rungs ────────────────────────────────
            old_rungs = [r for r in rungs if r['side'] == ls and not r.get('hold_to_res')]
            cap_freed = 0.0
            if active_sell:
                # sell all old-side rungs at bid where bid is liquid enough
                sold_now = []
                for r in old_rungs:
                    b = bid[ls]
                    if b >= EXIT_MIN_BID:
                        do_sell(ls, b, r['qty'])
                        cap_freed += r['cost']
                        sold_now.append(r)
                    else:
                        r['hold_to_res'] = True
                for r in sold_now:
                    rungs.remove(r)
                # rungs not sold are already marked hold_to_res above
            else:
                for r in old_rungs:
                    r['hold_to_res'] = True

            # ── compute recovery ──────────────────────────────────────────
            open_cost  = open_rung_cost(ls)  # remaining hold_to_res cost
            net_needed = open_cost + RECOVERY_TARGET

            if multi_flip:
                rec_budget = min(RECOVERY_BUDGET, flip_budget, available(),
                                 MAX_SIDE_COST - pos[other].cost)
            else:
                rec_budget = min(RECOVERY_BUDGET, available(),
                                 MAX_SIDE_COST - pos[other].cost)

            if rec_budget >= MIN_TRADE and other_price > 0.01:
                qty = do_buy(other, other_price, rec_budget)
                if qty:
                    sell_tgt    = min(other_price + net_needed / qty, MAX_RUNG_PRICE)
                    rec_side    = other
                    rec_qty     = qty
                    rec_target  = sell_tgt
                    phase       = 'flip_recover'
                    ladder_side = other
                    last_rung_px = other_price

                    if multi_flip:
                        flip_budget -= qty * other_price
                    else:
                        flipped_once = True

                    # reset flip-detect counter; start cooldown
                    flip_ticks = 0
                    if use_cooldown:
                        flip_cd_remaining = FLIP_COOLDOWN
                    return True

            # No recovery possible, just mark and continue
            if not multi_flip:
                flipped_once = True
            flip_ticks = 0
            return False

        # ── LADDER ───────────────────────────────────────────────────────────
        if phase == 'ladder':
            ls = ladder_side; ls_price = px[ls]

            if ls_price < FLIP_TRIGGER and leading != ls:
                flip_ticks += 1
            else:
                flip_ticks = 0

            if flip_ticks >= FLIP_CONFIRM and flip_cd_remaining == 0:
                try_flip(ls)

            if phase == 'ladder':
                oc = open_rung_count(ls)
                if (ls_price >= last_rung_px + RUNG_STEP
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS
                        and available() >= RUNG_USD
                        and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    qty = do_buy(ls, ls_price, RUNG_USD)
                    if qty:
                        target = round(ls_price + RUNG_SPREAD, 4)
                        stop_p = round(ls_price - sl_dist, 4)
                        rungs.append({
                            'side': ls, 'buy_price': ls_price,
                            'sell_target': target, 'stop_price': stop_p,
                            'qty': qty, 'cost': qty * ls_price, 'hold_to_res': False,
                        })
                        last_rung_px = ls_price

        # ── FLIP_RECOVER ─────────────────────────────────────────────────────
        elif phase == 'flip_recover':
            ls = ladder_side; ls_price = px[ls]

            # Check for another flip while recovering
            if ls_price < FLIP_TRIGGER and leading != ls:
                flip_ticks += 1
            else:
                flip_ticks = 0

            if flip_ticks >= FLIP_CONFIRM and flip_cd_remaining == 0:
                try_flip(ls)

            # Continue laddering on current side (if still flip_recover or ladder)
            if phase in ('flip_recover', 'ladder'):
                ls = ladder_side; ls_price = px[ls]
                oc = open_rung_count(ls)
                if (ls_price >= last_rung_px + RUNG_STEP
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS
                        and available() >= RUNG_USD
                        and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    qty = do_buy(ls, ls_price, RUNG_USD)
                    if qty:
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
# Stats
# ═══════════════════════════════════════════════════════════════════════════════

def stats_block(pnls, label, indent='  '):
    wins     = sum(1 for p in pnls if p > 0)
    losses   = sum(1 for p in pnls if p < 0)
    avg      = statistics.mean(pnls)
    std      = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    wr       = wins / len(pnls) * 100
    avg_win  = statistics.mean(p for p in pnls if p > 0) if wins  else 0
    avg_loss = statistics.mean(p for p in pnls if p < 0) if losses else 0
    rr       = abs(avg_win / avg_loss) if avg_loss else float('inf')
    sorted_p = sorted(pnls)
    p5       = sorted_p[int(0.05 * len(sorted_p))]
    p95      = sorted_p[int(0.95 * len(sorted_p))]
    print(f"{indent}{'─'*54}")
    print(f"{indent}{label}  (n={len(pnls):,})")
    print(f"{indent}{'─'*54}")
    print(f"{indent}Win / Loss        : {wins:,} / {losses:,}   WR={wr:.1f}%")
    print(f"{indent}Avg PnL / market  : ${avg:+.4f}   std=${std:.4f}")
    print(f"{indent}Avg win / avg loss: ${avg_win:+.4f} / ${avg_loss:+.4f}   R:R={rr:.2f}x")
    print(f"{indent}P5 / P95          : ${p5:+.4f} / ${p95:+.4f}")
    return avg


def run():
    print(f'Generating {N_MARKETS:,} market paths...')
    markets = [generate_market() for _ in range(N_MARKETS)]

    flip_counts = {i: 0 for i in range(6)}
    for *_, n in markets:
        flip_counts[min(n, 5)] += 1
    print(f'Flip distribution:')
    for k, v in sorted(flip_counts.items()):
        if v > 0:
            print(f'  {k} flip{"s" if k!=1 else ""}: {v:,}  ({v/N_MARKETS*100:.1f}%)')

    modes: List[Mode] = ['baseline', 'active_sell', 'multi_flip', 'combined',
                          'multi_flip_cd', 'combined_cd']
    labels = {
        'baseline':      'BASELINE      (hold_to_res, 1 flip max)',
        'active_sell':   'ACTIVE SELL   (sell old rungs at flip)',
        'multi_flip':    'MULTI FLIP    (budget pool, no cooldown)',
        'combined':      'COMBINED      (active sell + multi-flip)',
        'multi_flip_cd': f'MULTI FLIP+CD (budget pool + {FLIP_COOLDOWN}t cooldown)',
        'combined_cd':   f'COMBINED+CD   (active sell + multi-flip + {FLIP_COOLDOWN}t cd)',
    }

    print(f'\nRunning simulations...')
    results: dict = {m: [] for m in modes}
    for up, dn, out, _ in markets:
        for m in modes:
            results[m].append(simulate_market(up, dn, out, m))

    # ── overall ──────────────────────────────────────────────────────────────
    print(f"\n{'═'*58}")
    print(f"  OVERALL  ({N_MARKETS:,} markets)")
    print(f"{'═'*58}")
    avgs = {}
    for m in modes:
        avgs[m] = stats_block(results[m], labels[m])

    base = avgs['baseline']
    print(f"\n  {'Variant':<38}  {'Δ avg/mkt':>10}  {'Δ total':>12}  verdict")
    print(f"  {'─'*70}")
    for m in modes[1:]:
        d = avgs[m] - base
        verdict = '✅ BETTER' if d > 0.002 else ('❌ WORSE' if d < -0.002 else '➖ NEUTRAL')
        print(f"  {labels[m]:<38}  {d:>+10.4f}  ${d*N_MARKETS:>+10.2f}  {verdict}")

    # ── by flip count ─────────────────────────────────────────────────────────
    for flip_n in [0, 1, 2, 3, 4, 5]:
        idxs = [i for i, (_, _, _, n) in enumerate(markets) if n == flip_n]
        if len(idxs) < 10:
            continue
        label_n = f'{flip_n}-FLIP MARKETS  (n={len(idxs):,})'
        print(f"\n{'═'*58}")
        print(f"  {label_n}")
        print(f"{'═'*58}")
        avgs_n = {}
        for m in modes:
            sub = [results[m][i] for i in idxs]
            avgs_n[m] = stats_block(sub, labels[m])
        base_n = avgs_n['baseline']
        print(f"\n  {'Variant':<38}  {'Δ avg/mkt':>10}  verdict")
        print(f"  {'─'*55}")
        for m in modes[1:]:
            d = avgs_n[m] - base_n
            verdict = '✅' if d > 0.001 else ('❌' if d < -0.001 else '➖')
            print(f"  {labels[m]:<38}  {d:>+10.4f}  {verdict}")

    # ── summary bar chart ─────────────────────────────────────────────────────
    print(f"\n{'═'*58}")
    print(f"  SUMMARY — avg PnL/market")
    print(f"{'═'*58}")
    all_vals = list(avgs.values())
    max_val  = max(abs(v) for v in all_vals) or 1
    bw = 28
    for m in modes:
        v   = avgs[m]
        bar = ('█' if v >= 0 else '▒') * max(1, int(abs(v) / max_val * bw))
        print(f"  {labels[m]:<42}  ${v:>+7.4f}  {bar}")
    print()


if __name__ == '__main__':
    print(f'\nMonte Carlo: {N_MARKETS:,} markets — multi-flip strategies')
    print(f'FLIP_BUDGET=${FLIP_BUDGET}  RECOVERY_BUDGET=${RECOVERY_BUDGET}  FLIP_COOLDOWN={FLIP_COOLDOWN}t')
    print(f'RUNG_SPREAD={RUNG_SPREAD}  MIN_RR={MIN_RR}  EXIT_TTC={EXIT_TTC}s')
    print(f'Flip probs: {FLIP_PROBS}\n')
    run()
