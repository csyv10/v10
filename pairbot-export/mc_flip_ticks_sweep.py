"""
mc_flip_ticks_sweep.py
════════════════════════════════════════════════════════════════════════════
Sweep FLIP_CONFIRM_TICKS across [2, 3, 5, 7, 10] using the real
LadderMateStrategy class.

Both market models (Optimistic + Polymarket-realistic) are generated ONCE
and reused across all tick values so results are directly comparable.

Usage:
    python pair_engine_package/mc_flip_ticks_sweep.py
"""

import random, statistics, sys, os, io, contextlib
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from laddermate_strategy import LadderMateStrategy
from mc_laddermate import (
    generate_market_optimistic,
    generate_market_polymarket,
    N_MARKETS, N_TICKS,
)

random.seed(42)

TICK_VALUES = [2, 3, 5, 7, 10]

# ─── Market generation ─────────────────────────────────────────────────────────
print(f'Generating {N_MARKETS:,} market pairs (shared across all sweep values)…')
opt_markets  = [generate_market_optimistic()  for _ in range(N_MARKETS)]
poly_markets = [generate_market_polymarket()  for _ in range(N_MARKETS)]
print('  Done.\n')


# ─── Runner ────────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _suppress():
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        yield

def run_batch(markets, flip_ticks: int) -> dict:
    """Run all markets with a patched FLIP_CONFIRM_TICKS. Return aggregate stats."""
    # monkey-patch the class constant
    LadderMateStrategy.FLIP_CONFIRM_TICKS = flip_ticks

    pnls = []
    for up_prices, dn_prices, outcome in markets:
        strat = LadderMateStrategy()
        with _suppress():
            for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
                ttc = float(N_TICKS - t)
                strat.check_and_trade(
                    up_price=up, down_price=dn, timestamp=str(t),
                    time_to_close=ttc,
                    up_bid=round(up - 0.005, 4),
                    down_bid=round(dn - 0.005, 4),
                )
            pnl = strat.resolve_market(outcome)
        if strat.trade_count > 0:
            pnls.append(pnl)

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n      = len(pnls)
    ev     = sum(pnls) / n if n else 0.0
    wr     = 100 * len(wins) / n if n else 0.0

    avg_w  = statistics.mean(wins)   if wins   else 0.0
    avg_l  = statistics.mean(losses) if losses else 0.0
    worst  = min(pnls) if pnls else 0.0
    stdev  = statistics.stdev(pnls) if len(pnls) > 1 else 0.0

    return {
        'ticks':  flip_ticks,
        'n':      n,
        'wr':     wr,
        'ev':     ev,
        'avg_w':  avg_w,
        'avg_l':  avg_l,
        'worst':  worst,
        'stdev':  stdev,
    }


# ─── Sweep ─────────────────────────────────────────────────────────────────────
opt_rows  = []
poly_rows = []

for ticks in TICK_VALUES:
    print(f'  Running FLIP_CONFIRM_TICKS={ticks} …', flush=True)
    opt_rows.append(run_batch(opt_markets,  ticks))
    poly_rows.append(run_batch(poly_markets, ticks))
print()


# ─── Print ─────────────────────────────────────────────────────────────────────
def print_table(label: str, rows: list):
    SEP = '═' * 74
    HDR = ('TICKS  │  Winrate  │  EV/mkt  │  Avg Win  │  Avg Loss │'
           '  Worst   │  Stdev')
    print(f'\n{SEP}')
    print(f'  {label}')
    print(SEP)
    print(f'  {HDR}')
    print('  ' + '─' * 72)
    for r in rows:
        star = ' ◄' if r['ev'] == max(x['ev'] for x in rows) else '  '
        print(
            f"  {r['ticks']:>4}   │"
            f"  {r['wr']:>6.1f}%  │"
            f"  {r['ev']:>+7.4f} │"
            f"  {r['avg_w']:>+8.4f} │"
            f"  {r['avg_l']:>+8.4f} │"
            f"  {r['worst']:>+7.4f} │"
            f"  {r['stdev']:>.4f}"
            f"{star}"
        )
    print(SEP)

print_table('MODEL A — OPTIMISTIC  (35% flip, smooth drift)', opt_rows)
print_table('MODEL B — POLYMARKET  (50% flip, flat + news jumps)', poly_rows)

# Restore original value
LadderMateStrategy.FLIP_CONFIRM_TICKS = 3
