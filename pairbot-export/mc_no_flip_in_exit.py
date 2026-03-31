"""
mc_no_flip_in_exit.py
════════════════════════════════════════════════════════════════════════════
A/B Monte Carlo: test whether blocking flip execution inside the exit window
improves results vs the old behaviour (flip always fires).

FIX now implemented in laddermate_strategy.py:
  If flip fires while ttc < EXIT_TTC → suppress: reset _flip_ticks, log warning,
  let existing rungs run to stop-loss, rung-sell or resolution at $1.00.

A = old behaviour: EXIT_TTC overridden to 0 → flip guard never activates
B = new behaviour: EXIT_TTC = 50 (default) → flip suppressed in exit window

Usage:
    python pair_engine_package/mc_no_flip_in_exit.py
"""

import random, statistics, sys, os, io, contextlib
from typing import List

sys.path.insert(0, os.path.dirname(__file__))
from laddermate_strategy import LadderMateStrategy
from mc_laddermate import (
    generate_market_optimistic,
    generate_market_polymarket,
    N_MARKETS, N_TICKS,
)

random.seed(42)

LATE_FLIP_PROB = 0.25   # 25% of late-flip markets have a reversal in final 50s

print(f'Generating {N_MARKETS:,} shared market pairs…')
opt_markets       = [generate_market_optimistic()                        for _ in range(N_MARKETS)]
poly_markets      = [generate_market_polymarket()                        for _ in range(N_MARKETS)]
late_flip_markets = [generate_market_polymarket(late_flip_prob=LATE_FLIP_PROB) for _ in range(N_MARKETS)]
print('  Done.\n')


# ─── Runner ────────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _suppress():
    b = io.StringIO()
    with contextlib.redirect_stdout(b), contextlib.redirect_stderr(b):
        yield


def run_batch(markets, patch_flip_in_exit: bool) -> list:
    """
    Run all markets.
    patch_flip_in_exit=False → A: old behaviour — override EXIT_TTC=0 so the
                               exit-window flip guard never activates (flip always fires).
    patch_flip_in_exit=True  → B: new behaviour — EXIT_TTC=50 (default), flip
                               suppressed when ttc < EXIT_TTC.
    """
    results = []

    for up_prices, dn_prices, outcome in markets:
        strat = LadderMateStrategy()

        if not patch_flip_in_exit:
            # Simulate old behaviour: disable the exit-window flip guard
            # by setting EXIT_TTC to 0 (ttc is always >= 0, guard never fires)
            strat.EXIT_TTC = 0

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
            results.append(pnl)

    return results


# ─── Stats ────────────────────────────────────────────────────────────────────
def stats(pnls):
    if not pnls:
        return {}
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n      = len(pnls)
    ev     = sum(pnls) / n
    wr     = 100 * len(wins) / n
    avg_w  = statistics.mean(wins)   if wins   else 0.0
    avg_l  = statistics.mean(losses) if losses else 0.0
    worst  = min(pnls);  best = max(pnls)
    std    = statistics.stdev(pnls) if n > 1 else 0.0

    def pct(x): return x / (abs(x) + 1e-9) * 100
    kelly = 0.0
    if wins and losses:
        wl = avg_w / abs(avg_l)
        w  = len(wins) / n
        kelly = w - (1 - w) / wl

    return dict(n=n, wr=wr, ev=ev, avg_w=avg_w, avg_l=avg_l,
                worst=worst, best=best, std=std, kelly=kelly,
                total=sum(pnls))


def print_comparison(label_a, pa, label_b, pb):
    sa = stats(pa)
    sb = stats(pb)
    SEP = '═' * 72
    HDR = f'  {"Metric":<22}  {"Flip-always (A)":<22}  {"Flip-suppressed (B)":<22}'
    print(f'\n{SEP}')
    print(f'  {label_a}')
    print(SEP)
    print(HDR)
    print('  ' + '─' * 70)

    def row(name, key, fmt='.4f', delta_fmt='+.4f', sign=''):
        va = sa.get(key, 0); vb = sb.get(key, 0)
        d  = vb - va
        star = ' ◄ better' if (key in ('wr','ev','worst','kelly') and d > 0) \
          else ' ◄ better' if (key == 'worst' and d > 0) \
          else ' ◄ better' if (key == 'std'   and d < 0) \
          else ''
        # worst: higher (less negative) is better
        if key == 'worst':
            star = ' ◄ better' if d > 0 else ''
        if key == 'std':
            star = ' ◄ better' if d < 0 else ''
        print(f'  {name:<22}  {sign}{va:{fmt}}{"":<12}  {sign}{vb:{fmt}}{"":<5}'
              f'  (Δ{d:{delta_fmt}}){star}')

    row('Winrate (%)',   'wr',    '.2f', '+.2f')
    row('EV/market ($)', 'ev',    '+.4f', '+.4f')
    row('Total PnL ($)', 'total', '+.2f', '+.2f')
    row('Worst case ($)','worst', '+.4f', '+.4f')
    row('Best case ($)', 'best',  '+.4f', '+.4f')
    row('Stdev',         'std',   '.4f',  '+.4f')
    row('Kelly',         'kelly', '+.4f', '+.4f')
    row('Avg Win ($)',   'avg_w', '+.4f', '+.4f')
    row('Avg Loss ($)',  'avg_l', '+.4f', '+.4f')
    print(SEP)


# ─── Main ─────────────────────────────────────────────────────────────────────
print('Running A (baseline) + B (no flip restart in exit window) …')
print('  Optimistic A…',  end=' ', flush=True)
opt_a  = run_batch(opt_markets,  patch_flip_in_exit=False)
print('done')
print('  Optimistic B…',  end=' ', flush=True)
opt_b  = run_batch(opt_markets,  patch_flip_in_exit=True)
print('done')
print('  Polymarket A…',  end=' ', flush=True)
poly_a = run_batch(poly_markets, patch_flip_in_exit=False)
print('done')
print('  Polymarket B…',  end=' ', flush=True)
poly_b = run_batch(poly_markets, patch_flip_in_exit=True)
print('done')
print(f'  Late-Flip Poly A (late_flip_prob={LATE_FLIP_PROB})…', end=' ', flush=True)
late_a = run_batch(late_flip_markets, patch_flip_in_exit=False)
print('done')
print(f'  Late-Flip Poly B (late_flip_prob={LATE_FLIP_PROB})…', end=' ', flush=True)
late_b = run_batch(late_flip_markets, patch_flip_in_exit=True)
print('done\n')

print_comparison('MODEL A — OPTIMISTIC  (35% flip, smooth drift)  | A=flip-always  B=flip-suppressed', opt_a, '', opt_b)
print_comparison('MODEL B — POLYMARKET  (50% flip, news jumps)    | A=flip-always  B=flip-suppressed', poly_a, '', poly_b)
print_comparison(f'MODEL C — LATE-FLIP POLY  (late_flip={LATE_FLIP_PROB}) | A=flip-always  B=flip-suppressed', late_a, '', late_b)
