"""
mc_laddermate.py
════════════════════════════════════════════════════════════════════════════
Monte Carlo: 10 000 markets using the real LadderMateStrategy class.

Runs TWO market models side-by-side:
  MODEL A — Optimistic (smooth trending, gradual drift)
            Similar to original MC assumption. Best-case for the strategy.

  MODEL B — Polymarket-realistic
            * Markets open near 50/50 with tiny initial spread
            * Very low continuous drift — price is mostly noise / random walk
            * Occasional news-driven JUMP events (+/- 8–20 cents instantly)
            * Outcome only "locks in" in the final 20% of the market lifetime
            * Higher flip probability (50% vs 35%)
            * Harder entry window: price rarely trends into [0.28, 0.60]
              neatly — it either spikes past or stays flat at 0.50

Usage:
    python pair_engine_package/mc_laddermate.py
"""

import random
import statistics
import sys
import os
import io
import contextlib
from typing import List, Tuple

# Allow running from repo root
sys.path.insert(0, os.path.dirname(__file__))
from laddermate_strategy import LadderMateStrategy

random.seed(42)

# ─── Shared ───────────────────────────────────────────────────────────────────
N_MARKETS = 10_000
N_TICKS   = 300


# ─── Model A: Optimistic (original) ──────────────────────────────────────────
MA_FLIP_PROB      = 0.35
MA_FLIP_WINDOW    = (60, 220)
MA_NOISE_SIGMA    = 0.008
MA_DRIFT_STRENGTH = 0.004
MA_OUTCOME_PRICE  = 0.85
MA_LOSER_PRICE    = 0.15
MA_OPEN_SPREAD    = 0.04

def generate_market_optimistic() -> Tuple[List[float], List[float], str]:
    """Smooth trending market — best case for a ladder strategy."""
    does_flip = random.random() < MA_FLIP_PROB
    if does_flip:
        flip_tick     = random.randint(*MA_FLIP_WINDOW)
        first_winner  = 'UP' if random.random() < 0.5 else 'DOWN'
        second_winner = 'DOWN' if first_winner == 'UP' else 'UP'
        final_outcome = second_winner
    else:
        flip_tick     = N_TICKS + 1
        final_outcome = 'UP' if random.random() < 0.5 else 'DOWN'
        first_winner  = final_outcome
        second_winner = final_outcome

    if first_winner == 'UP':
        up0 = 0.50 + MA_OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 - MA_OPEN_SPREAD / 2 + random.gauss(0, 0.01)
    else:
        up0 = 0.50 - MA_OPEN_SPREAD / 2 + random.gauss(0, 0.01)
        dn0 = 0.50 + MA_OPEN_SPREAD / 2 + random.gauss(0, 0.01)

    up_prices = [max(0.05, min(0.95, up0))]
    dn_prices = [max(0.05, min(0.95, dn0))]

    for t in range(1, N_TICKS):
        winner_now = first_winner if t < flip_tick else second_winner
        progress   = t / N_TICKS
        ds         = MA_DRIFT_STRENGTH * (0.5 + progress)
        up_tgt = MA_OUTCOME_PRICE if winner_now == 'UP' else MA_LOSER_PRICE
        dn_tgt = MA_OUTCOME_PRICE if winner_now == 'DOWN' else MA_LOSER_PRICE
        up_new = up_prices[-1] + ds * (up_tgt - up_prices[-1]) + random.gauss(0, MA_NOISE_SIGMA)
        dn_new = dn_prices[-1] + ds * (dn_tgt - dn_prices[-1]) + random.gauss(0, MA_NOISE_SIGMA)
        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.15:
            corr = (mid - 0.5) * 0.3
            up_new -= corr; dn_new -= corr
        up_prices.append(max(0.02, min(0.98, up_new)))
        dn_prices.append(max(0.02, min(0.98, dn_new)))

    return up_prices, dn_prices, final_outcome


# ─── Model B: Polymarket-realistic ───────────────────────────────────────────
# Polymarket binary markets behave very differently:
#   - Open near 0.50/0.50, tiny initial spread
#   - Mostly flat / random walk — no steady drift from tick to tick
#   - News-driven JUMPS: sudden 8–20 cent moves on a single tick
#   - Locks in toward resolution only in the last ~20% of ticks
#   - ~50% of markets flip at some point (uncertain fundamentals)
#   - Prices can sit at 0.48/0.52 for 80% of the market then jump to 0.85/0.15
MB_FLIP_PROB        = 0.50    # more uncertain markets
MB_FLIP_WINDOW      = (40, 240)
MB_NOISE_SIGMA      = 0.012   # higher tick-to-tick noise
MB_DRIFT_STRENGTH   = 0.0005  # almost no continuous drift — mostly flat
MB_FINAL_DRIFT      = 0.025   # strong resolution pull in last 20% of ticks
MB_OUTCOME_PRICE    = 0.88
MB_LOSER_PRICE      = 0.12
MB_OPEN_SPREAD      = 0.01    # starts very tight at 50/50
MB_JUMP_PROB        = 0.025   # 2.5% chance of a news jump each tick
MB_JUMP_SIZE_MIN    = 0.06    # smallest jump
MB_JUMP_SIZE_MAX    = 0.18    # largest jump
MB_RESOLUTION_START = 0.80    # final resolution drift kicks in at 80% of ticks
MB_LATE_FLIP_PROB   = 0.0     # extra flip inside exit window (TTC < EXIT_TTC); default off
MB_LATE_FLIP_WINDOW = (250, 290)  # tick range → TTC ≈ 10–50 s

def generate_market_polymarket(late_flip_prob: float = 0.0) -> Tuple[List[float], List[float], str]:
    """
    Polymarket-realistic price path.
    Flat/noisy most of the time, punctuated by news jumps,
    resolving only late in the market lifetime.
    """
    does_flip = random.random() < MB_FLIP_PROB
    if does_flip:
        flip_tick     = random.randint(*MB_FLIP_WINDOW)
        first_winner  = 'UP' if random.random() < 0.5 else 'DOWN'
        second_winner = 'DOWN' if first_winner == 'UP' else 'UP'
    else:
        flip_tick     = N_TICKS + 1
        first_winner  = 'UP' if random.random() < 0.5 else 'DOWN'
        second_winner = first_winner

    # Late flip: an extra reversal inside the exit window (TTC < EXIT_TTC)
    effective_late_prob = late_flip_prob if late_flip_prob > 0.0 else MB_LATE_FLIP_PROB
    does_late_flip = random.random() < effective_late_prob
    if does_late_flip:
        late_flip_tick = random.randint(*MB_LATE_FLIP_WINDOW)
        third_winner   = 'DOWN' if second_winner == 'UP' else 'UP'
        final_outcome  = third_winner
    else:
        late_flip_tick = N_TICKS + 1
        third_winner   = second_winner
        final_outcome  = second_winner

    # Open very close to 50/50
    up0 = 0.50 + MB_OPEN_SPREAD / 2 + random.gauss(0, 0.005)
    dn0 = 0.50 - MB_OPEN_SPREAD / 2 + random.gauss(0, 0.005)

    up_prices = [max(0.05, min(0.95, up0))]
    dn_prices = [max(0.05, min(0.95, dn0))]

    for t in range(1, N_TICKS):
        if t < flip_tick:
            winner_now = first_winner
        elif t < late_flip_tick:
            winner_now = second_winner
        else:
            winner_now = third_winner
        progress   = t / N_TICKS

        up_tgt = MB_OUTCOME_PRICE if winner_now == 'UP'   else MB_LOSER_PRICE
        dn_tgt = MB_OUTCOME_PRICE if winner_now == 'DOWN' else MB_LOSER_PRICE

        # Drift: near-zero until final resolution window, then strong pull
        if progress >= MB_RESOLUTION_START:
            ds = MB_FINAL_DRIFT * ((progress - MB_RESOLUTION_START) / (1 - MB_RESOLUTION_START))
        else:
            ds = MB_DRIFT_STRENGTH

        up_new = up_prices[-1] + ds * (up_tgt - up_prices[-1]) + random.gauss(0, MB_NOISE_SIGMA)
        dn_new = dn_prices[-1] + ds * (dn_tgt - dn_prices[-1]) + random.gauss(0, MB_NOISE_SIGMA)

        # Late-flip forced jump: at the late_flip_tick, price reverses hard
        # enough to cross FLIP_TRIGGER (0.50) in a single tick so the strategy
        # detects the flip within FLIP_CONFIRM_TICKS (2) ticks.
        if t == late_flip_tick:
            # Determine magnitude: we need new loser to drop below 0.45 and
            # new winner to rise above 0.55 regardless of current prices.
            forced_jump = max(0.45, abs(up_prices[-1] - 0.45) + 0.05,
                              abs(dn_prices[-1] - 0.45) + 0.05)
            if third_winner == 'UP':   # was DOWN, flips to UP
                up_new += forced_jump; dn_new -= forced_jump
            else:                      # was UP, flips to DOWN
                dn_new += forced_jump; up_new -= forced_jump
        # News jump: moves winner up, loser down by same amount
        elif random.random() < MB_JUMP_PROB:
            jump = random.uniform(MB_JUMP_SIZE_MIN, MB_JUMP_SIZE_MAX)
            if winner_now == 'UP':
                up_new += jump; dn_new -= jump
            else:
                dn_new += jump; up_new -= jump

        # Soft normalization
        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.12:
            corr = (mid - 0.5) * 0.25
            up_new -= corr; dn_new -= corr

        up_prices.append(max(0.02, min(0.98, up_new)))
        dn_prices.append(max(0.02, min(0.98, dn_new)))

    return up_prices, dn_prices, final_outcome


# ─── Run a single market through the real strategy ────────────────────────────
def run_market(up_prices: List[float], dn_prices: List[float], outcome: str) -> dict:
    strat = LadderMateStrategy(starting_balance=1000.0)

    with contextlib.redirect_stdout(io.StringIO()):
        for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
            ttc = float(N_TICKS - t)
            strat.check_and_trade(
                up_price=up, down_price=dn, timestamp=str(t),
                time_to_close=ttc,
                up_bid=round(up - 0.005, 4),
                down_bid=round(dn - 0.005, 4),
            )
        pnl = strat.resolve_market(outcome)

    return {
        'pnl':         pnl,
        'traded':      strat.trade_count > 0,
        'trade_count': strat.trade_count,
        'cash_out':    strat.cash_out,
    }


# ─── Stats helpers ────────────────────────────────────────────────────────────
def pct(x, total): return f'{100 * x / total:.1f}%' if total else '0.0%'

def percentile(data: List[float], p: float) -> float:
    if not data: return 0.0
    s = sorted(data); idx = (len(s) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)

def print_stats(label: str, results: list):
    traded    = [r for r in results if r['traded']]
    skipped   = [r for r in results if not r['traded']]
    wins      = [r for r in traded if r['pnl'] > 0]
    losses    = [r for r in traded if r['pnl'] < 0]
    n         = len(traded); tot = len(results)
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
    print(f'  │  Traded   : {n:>6,} / {tot:,}  skipped {len(skipped):,} ({pct(len(skipped),tot)}){"":>8}│')
    print(f'  │  Winrate  : {pct(len(wins),n):>6}  ({len(wins):,}W / {len(losses):,}L){"":>20}│')
    print(f'  │  Total PnL: {total_pnl:>+10.2f} USD{"":>32}│')
    print(f'  │  EV/market: {ev:>+10.4f} USD{"":>32}│')
    if all_pnls:
        print(f'  │  Median   : {statistics.median(all_pnls):>+10.4f} USD{"":>32}│')
        print(f'  │  Stdev    : {statistics.stdev(all_pnls):>10.4f} USD{"":>32}│')
        print(f'  │  Best/Worst: {max(all_pnls):>+8.4f} / {min(all_pnls):>+8.4f} USD{"":>15}│')
        print(f'  │  P10/P90  : {percentile(all_pnls,10):>+8.4f} / {percentile(all_pnls,90):>+8.4f} USD{"":>15}│')
    if win_pnls and loss_pnls:
        avg_w = statistics.mean(win_pnls); avg_l = abs(statistics.mean(loss_pnls))
        wl = avg_w / avg_l; w = len(wins) / n
        kelly = w - (1 - w) / wl
        print(f'  │  Avg W/L  : {avg_w:>+8.4f} / {statistics.mean(loss_pnls):>+8.4f} USD{"":>15}│')
        print(f'  │  W/L ratio: {wl:>10.3f}x{"":>35}│')
        print(f'  │  Kelly    : {kelly:>+10.3f}{"":>41}│')
    if tcs:
        print(f'  │  Avg trades: {statistics.mean(tcs):>9.1f}{"":>35}│')
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


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    strat_cfg = (f'LOSS_CAP={LadderMateStrategy.LOSS_CAP}  '
                 f'RUNG_USD={LadderMateStrategy.RUNG_USD}  '
                 f'RUNG_SPREAD={LadderMateStrategy.RUNG_SPREAD}  '
                 f'RUNG_STEP={LadderMateStrategy.RUNG_STEP}  '
                 f'FLIP_BUDGET={LadderMateStrategy.FLIP_BUDGET}')
    print(f'Strategy: {strat_cfg}')
    print(f'Generating {N_MARKETS:,} market pairs (optimistic + polymarket)...')

    opt_results  = []
    poly_results = []

    for i in range(N_MARKETS):
        if i % 2000 == 0 and i > 0:
            print(f'  ... {i:,} done', flush=True)
        up_o, dn_o, out_o = generate_market_optimistic()
        up_p, dn_p, out_p = generate_market_polymarket()
        opt_results.append(run_market(up_o, dn_o, out_o))
        poly_results.append(run_market(up_p, dn_p, out_p))

    sep = '═' * 62
    print(f'\n{sep}')
    print('  LADDERMATE MONTE CARLO  —  two market models, 10 000 each')
    print(sep)
    print(f'\n  Model A — Optimistic (smooth drift, 35% flip rate):')
    print(f'    DRIFT={MA_DRIFT_STRENGTH}  NOISE={MA_NOISE_SIGMA}  OPEN_SPREAD={MA_OPEN_SPREAD}')
    print(f'\n  Model B — Polymarket-realistic (flat + news jumps, 50% flip rate):')
    print(f'    DRIFT={MB_DRIFT_STRENGTH}  NOISE={MB_NOISE_SIGMA}  JUMP_PROB={MB_JUMP_PROB}'
          f'  JUMP={MB_JUMP_SIZE_MIN}–{MB_JUMP_SIZE_MAX}  LATE_RESOLVE@{int(MB_RESOLUTION_START*100)}%')

    print_stats('A — OPTIMISTIC  (best-case / original model)', opt_results)
    print()
    print_stats('B — POLYMARKET-REALISTIC  (flat + jumps + late resolution)', poly_results)

    # Delta
    a_pnls = [r['pnl'] for r in opt_results  if r['traded']]
    b_pnls = [r['pnl'] for r in poly_results if r['traded']]
    a_wins = sum(1 for p in a_pnls if p > 0)
    b_wins = sum(1 for p in b_pnls if p > 0)
    dev    = (sum(b_pnls)/len(b_pnls) if b_pnls else 0) - (sum(a_pnls)/len(a_pnls) if a_pnls else 0)
    dwr    = (b_wins/len(b_pnls)*100 if b_pnls else 0) - (a_wins/len(a_pnls)*100 if a_pnls else 0)
    dtot   = sum(b_pnls) - sum(a_pnls)

    print(f'\n{sep}')
    print('  REALITY DISCOUNT  (Polymarket − Optimistic)')
    print(sep)
    print(f'  EV/market  : {dev:>+8.4f} USD')
    print(f'  Winrate    : {dwr:>+8.2f} pp')
    print(f'  Total PnL  : {dtot:>+8.2f} USD  (over {N_MARKETS:,} markets)')
    print(f'{sep}')


if __name__ == '__main__':
    main()
