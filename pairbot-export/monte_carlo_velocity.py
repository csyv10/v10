"""
monte_carlo_velocity.py
════════════════════════════════════════════════════════════════════════════
Monte Carlo simulation: 10 000 markets
Parameter grid search: LOSS_CAP × RUNG_SPREAD combinations.

Market model
  - 300 ticks (1 per second, ~5-minute market)
  - Price path: correlated random walk with drift toward final outcome
  - Realistic flip scenarios: ~35% of markets flip direction mid-way
  - Bid = ask - 0.005 (thin but realistic spread)
"""

import random
import statistics
from typing import List, Tuple

random.seed(42)

# ─── Market simulation parameters ────────────────────────────────────────────
N_MARKETS       = 10_000
N_TICKS         = 300
FLIP_PROB       = 0.35
FLIP_WINDOW     = (60, 220)
NOISE_SIGMA     = 0.008
DRIFT_STRENGTH  = 0.003
OUTCOME_PRICE   = 0.85
LOSER_PRICE     = 0.15
OPEN_SPREAD     = 0.04

# ─── Strategy parameters (matching laddermate_strategy.py) ───────────────────
ENTRY_MIN       = 0.28
ENTRY_MAX       = 0.60
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

# ─── Grid search ranges ──────────────────────────────────────────────────────
GRID_LOSS_CAP    = [3.00, 2.00, 1.50, 1.00]          # current = 3.00
GRID_RUNG_SPREAD = [0.020, 0.030, 0.040, 0.050]      # current = 0.020


# ═══════════════════════════════════════════════════════════════════════════════
# Price path generator
# ═══════════════════════════════════════════════════════════════════════════════

def generate_market() -> Tuple[List[float], List[float], str]:
    """
    Returns (up_prices, down_prices, final_outcome).
    Prices are realistic correlated random walks with drift toward outcome.
    ~35% of markets flip direction.
    """
    does_flip = random.random() < FLIP_PROB
    if does_flip:
        flip_tick = random.randint(*FLIP_WINDOW)
        # First half: UP leads → flips to DOWN wins
        if random.random() < 0.5:
            first_winner, second_winner = 'UP', 'DOWN'
        else:
            first_winner, second_winner = 'DOWN', 'UP'
        final_outcome = second_winner
    else:
        flip_tick = N_TICKS + 1
        final_outcome = 'UP' if random.random() < 0.5 else 'DOWN'
        first_winner = final_outcome
        second_winner = final_outcome

    # Opening prices: leading side slightly above 0.50
    if first_winner == 'UP':
        up0 = 0.50 + OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 - OPEN_SPREAD / 2 + random.gauss(0, 0.01)
    else:
        up0 = 0.50 - OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 + OPEN_SPREAD / 2 + random.gauss(0, 0.01)

    up0 = max(0.05, min(0.95, up0))
    dn0 = max(0.05, min(0.95, dn0))

    up_prices  = [up0]
    dn_prices  = [dn0]

    for t in range(1, N_TICKS):
        if t < flip_tick:
            winner_now = first_winner
        else:
            winner_now = second_winner

        # Current winner drifts toward OUTCOME_PRICE, loser toward LOSER_PRICE
        # Drift is stronger closer to resolution
        progress = t / N_TICKS
        drift_scale = DRIFT_STRENGTH * (0.5 + progress)

        up_target = OUTCOME_PRICE if winner_now == 'UP' else LOSER_PRICE
        dn_target = OUTCOME_PRICE if winner_now == 'DOWN' else LOSER_PRICE

        up_new = up_prices[-1] + drift_scale * (up_target - up_prices[-1]) + random.gauss(0, NOISE_SIGMA)
        dn_new = dn_prices[-1] + drift_scale * (dn_target - dn_prices[-1]) + random.gauss(0, NOISE_SIGMA)

        # Keep them from summing too far from 1 (binary market constraint)
        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.15:
            correction = (mid - 0.5) * 0.3
            up_new -= correction
            dn_new -= correction

        up_prices.append(max(0.02, min(0.98, up_new)))
        dn_prices.append(max(0.02, min(0.98, dn_new)))

    return up_prices, dn_prices, final_outcome


# ═══════════════════════════════════════════════════════════════════════════════
# Minimal position tracker
# ═══════════════════════════════════════════════════════════════════════════════

class _Pos:
    def __init__(self):
        self.qty  = 0.0
        self.cost = 0.0

    @property
    def avg(self):
        return self.cost / self.qty if self.qty > 0.001 else 0.0

    def add(self, qty, price):
        self.cost += qty * price
        self.qty  += qty

    def remove(self, qty) -> float:
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
# Simulate one market with the given variant
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_market(up_prices, dn_prices, outcome,
                    loss_cap: float, rung_spread: float) -> float:
    """Returns final PnL. Parameterised by loss_cap and rung_spread."""

    pos   = {'UP': _Pos(), 'DOWN': _Pos()}
    rungs = []          # list of dicts
    cash_out = 0.0
    cash_in  = 0.0
    realised = 0.0

    phase        = 'scout'
    ladder_side  = None
    flip_ticks   = 0
    flipped_once = False
    last_rung_px = 0.0

    rec_side   = None
    rec_qty    = 0.0
    rec_target = 0.0

    def available():
        spent = pos['UP'].cost + pos['DOWN'].cost
        return max(0.0, min(1000.0 - spent, 250.0 - spent))

    def loss_room(side, price):
        other_outcome = 'DOWN' if side == 'UP' else 'UP'
        pnl_other = pos[other_outcome].qty - (pos['UP'].cost + pos['DOWN'].cost) + realised
        return max(0.0, pnl_other + loss_cap)

    def do_buy(side, price, usd):
        nonlocal cash_out
        room = min(loss_room(side, price), available())
        usd  = min(usd, room)
        if usd < MIN_TRADE or price <= 0.01:
            return None
        qty = usd / price
        cash_out      += usd
        pos[side].add(qty, price)
        return qty

    def do_sell(side, price, qty):
        nonlocal cash_in, realised
        qty = min(qty, pos[side].qty)
        if qty < 0.001:
            return
        cb  = pos[side].remove(qty)
        prc = qty * price
        cash_in  += prc
        realised += prc - cb

    def open_rung_count(side):
        return sum(1 for r in rungs if r['side'] == side)

    def open_rung_cost(side):
        return sum(r['cost'] for r in rungs if r['side'] == side)

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        bid_up  = max(0.01, up  - 0.005)
        bid_dn  = max(0.01, dn  - 0.005)
        bid     = {'UP': bid_up, 'DOWN': bid_dn}
        px      = {'UP': up, 'DOWN': dn}
        leading = 'UP' if up >= dn else 'DOWN'

        # ── EXIT WINDOW ──────────────────────────────────────────────────────
        if ttc < EXIT_TTC:
            sold = []
            for r in rungs:
                if r.get('hold_to_res'):
                    continue
                s = r['side']
                if bid[s] >= EXIT_MIN_BID:
                    do_sell(s, bid[s], r['qty'])
                    sold.append(r)
            for r in sold:
                rungs.remove(r)

        if ttc < 10:
            continue

        sl_dist = round(rung_spread / MIN_RR, 4)

        # ── SELL CHECK (TP + SL) ─────────────────────────────────────────────
        sold = []
        for r in rungs:
            if r.get('hold_to_res'):
                continue
            s  = r['side']
            cp = px[s]
            if cp >= r['sell_target']:
                do_sell(s, cp, r['qty'])
                sold.append(r)
            elif cp <= r['stop_price']:
                do_sell(s, cp, r['qty'])
                sold.append(r)
        for r in sold:
            rungs.remove(r)

        # ── RECOVERY SELL ────────────────────────────────────────────────────
        if rec_side and rec_qty > 0.01:
            if px[rec_side] >= rec_target:
                do_sell(rec_side, px[rec_side], pos[rec_side].qty)
                rec_qty  = 0.0
                rec_side = None
                if ladder_side:
                    phase = 'ladder'

        # ── SCOUT ────────────────────────────────────────────────────────────
        if phase == 'scout':
            lp = px[leading]
            if ENTRY_MIN <= lp <= ENTRY_MAX:
                qty = do_buy(leading, lp, RUNG_USD)
                if qty:
                    rungs.append({
                        'side': leading, 'buy_price': lp,
                        'sell_target': round(lp + rung_spread, 4),
                        'stop_price':  round(lp - sl_dist, 4),
                        'qty': qty, 'cost': qty * lp, 'hold_to_res': False,
                    })
                    ladder_side   = leading
                    last_rung_px  = lp
                    phase         = 'ladder'
            continue

        # ── LADDER PHASE ─────────────────────────────────────────────────────
        if phase == 'ladder':
            ls       = ladder_side
            ls_price = px[ls]
            other    = 'DOWN' if ls == 'UP' else 'UP'

            # flip detection
            if ls_price < FLIP_TRIGGER and leading != ls:
                flip_ticks += 1
            else:
                flip_ticks  = 0

            if flip_ticks >= FLIP_CONFIRM and not flipped_once:
                flipped_once = True
                other_price  = px[other]
                for r in rungs:
                    if r['side'] == ls:
                        r['hold_to_res'] = True
                open_cost  = open_rung_cost(ls)
                net_needed = open_cost + RECOVERY_TARGET
                rec_budget = min(RECOVERY_BUDGET, available(),
                                 MAX_SIDE_COST - pos[other].cost)
                if rec_budget >= MIN_TRADE and other_price > 0.01:
                    qty = do_buy(other, other_price, rec_budget)
                    if qty:
                        sell_tgt      = min(other_price + net_needed / qty, MAX_RUNG_PRICE)
                        rec_side      = other
                        rec_qty       = qty
                        rec_target    = sell_tgt
                        phase         = 'flip_recover'
                        ladder_side   = other
                        last_rung_px  = other_price

            # add new rungs
            if phase == 'ladder':
                oc = open_rung_count(ls)
                if (ls_price >= last_rung_px + RUNG_STEP
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < MAX_RUNGS
                        and available() >= RUNG_USD
                        and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                    qty = do_buy(ls, ls_price, RUNG_USD)
                    if qty:
                        target = round(ls_price + rung_spread, 4)
                        stop_p = round(ls_price - sl_dist, 4)
                        rungs.append({
                            'side': ls, 'buy_price': ls_price,
                            'sell_target': target, 'stop_price': stop_p,
                            'qty': qty, 'cost': qty * ls_price, 'hold_to_res': False,
                        })
                        last_rung_px = ls_price

        elif phase == 'flip_recover':
            ls       = ladder_side
            ls_price = px[ls]
            oc       = open_rung_count(ls)
            if (ls_price >= last_rung_px + RUNG_STEP
                    and ls_price <= MAX_RUNG_PRICE
                    and oc < MAX_RUNGS
                    and available() >= RUNG_USD
                    and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST):
                qty = do_buy(ls, ls_price, RUNG_USD)
                if qty:
                    target = round(ls_price + rung_spread, 4)
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
# Run simulation — grid search
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    # Pre-generate all markets so every config sees identical price paths
    print(f'Generating {N_MARKETS:,} market paths...')
    markets = [generate_market() for _ in range(N_MARKETS)]

    # ── result table storage ─────────────────────────────────────────────────
    results = []   # list of dicts

    total_configs = len(GRID_LOSS_CAP) * len(GRID_RUNG_SPREAD)
    done = 0
    for lc in GRID_LOSS_CAP:
        for rs in GRID_RUNG_SPREAD:
            pnls = [
                simulate_market(up, dn, out, loss_cap=lc, rung_spread=rs)
                for up, dn, out in markets
            ]
            wins   = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            total  = sum(pnls)
            avg    = statistics.mean(pnls)
            std    = statistics.stdev(pnls)
            wr     = wins / len(pnls) * 100
            avg_win  = statistics.mean(p for p in pnls if p > 0) if wins else 0
            avg_loss = statistics.mean(p for p in pnls if p < 0) if losses else 0
            rr       = abs(avg_win / avg_loss) if avg_loss else float('inf')
            sorted_p = sorted(pnls)
            p5  = sorted_p[int(0.05 * len(sorted_p))]
            p95 = sorted_p[int(0.95 * len(sorted_p))]
            worst = min(pnls)
            is_baseline = (lc == 3.00 and rs == 0.020)
            results.append(dict(
                lc=lc, rs=rs, total=total, avg=avg, std=std,
                wr=wr, rr=rr, wins=wins, losses=losses,
                avg_win=avg_win, avg_loss=avg_loss,
                p5=p5, p95=p95, worst=worst,
                baseline=is_baseline,
            ))
            done += 1
            tag = ' ← BASELINE' if is_baseline else ''
            print(f'  [{done:>2}/{total_configs}] LOSS_CAP={lc:.2f}  '
                  f'RUNG_SPREAD={rs:.3f}  '
                  f'avg=${avg:+.4f}  WR={wr:.1f}%  R:R={rr:.2f}x{tag}')

    # ── sorted summary table ─────────────────────────────────────────────────
    results.sort(key=lambda r: r['avg'], reverse=True)
    baseline = next(r for r in results if r['baseline'])

    print(f"\n{'═'*75}")
    print(f"  GRID SEARCH RESULTS — sorted by avg PnL/market  ({N_MARKETS:,} markets)")
    print(f"{'═'*75}")
    hdr = f"  {'LOSS_CAP':>8}  {'SPREAD':>7}  {'Avg$/mkt':>9}  "\
          f"{'Total$':>10}  {'WR%':>5}  {'R:R':>5}  {'Std':>6}  {'P5':>7}  {'P95':>7}  {'Worst':>7}"
    print(hdr)
    print(f"  {'-'*8}  {'-'*7}  {'-'*9}  {'-'*10}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*7}  {'-'*7}")
    for r in results:
        tag  = ' ◄ BASELINE' if r['baseline'] else ''
        delta = r['avg'] - baseline['avg']
        dsign = f"+{delta:.4f}" if delta >= 0 else f"{delta:.4f}"
        mark = f'  Δ={dsign}' if not r['baseline'] else ''
        print(f"  {r['lc']:>8.2f}  {r['rs']:>7.3f}  "
              f"${r['avg']:>+8.4f}  "
              f"${r['total']:>+9.2f}  "
              f"{r['wr']:>5.1f}%  "
              f"{r['rr']:>5.2f}x  "
              f"${r['std']:>5.4f}  "
              f"${r['p5']:>+6.4f}  "
              f"${r['p95']:>+6.4f}  "
              f"${r['worst']:>+6.2f}"
              f"{tag}{mark}")

    best = results[0]
    print(f"\n{'═'*75}")
    print(f"  🏆 BEST CONFIG:  LOSS_CAP={best['lc']:.2f}  RUNG_SPREAD={best['rs']:.3f}")
    delta_avg   = best['avg']   - baseline['avg']
    delta_total = best['total'] - baseline['total']
    print(f"     vs baseline:  avg ${delta_avg:+.4f}/market  "
          f"(${delta_total:+,.2f} over {N_MARKETS:,} markets)")
    print(f"{'═'*75}\n")


if __name__ == '__main__':
    print(f'\nRunning Monte Carlo grid search: {N_MARKETS:,} markets...')
    print(f'LOSS_CAP grid:    {GRID_LOSS_CAP}')
    print(f'RUNG_SPREAD grid: {GRID_RUNG_SPREAD}\n')
    run()
