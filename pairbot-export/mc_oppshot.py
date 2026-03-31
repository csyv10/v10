"""
mc_oppshot.py
═════════════════════════════════════════════════════════════════════════════
Monte Carlo: 5 000 markets using OppShotStrategy (opportunist).

Runs TWO market models:
  MODEL A — Optimistic  : smooth trending, clear winner from early on,
                          35% flip probability.
  MODEL B — Polymarket  : opens 50/50, mostly flat / random walk,
                          news-driven jumps, resolves only in final 20%,
                          50% flip probability.

Usage:
    python pair_engine_package/mc_oppshot.py
"""

import random
import statistics
import sys
import os
import io
import contextlib
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
from opportunist_strategy import OppShotStrategy

random.seed(42)

N_MARKETS        = 5_000
N_TICKS          = 300   # ~5-minute market at 1 tick/sec
MARKET_BUDGET    = 150.0
STARTING_BALANCE = 1_000.0
TTC_START        = float(N_TICKS)   # seconds-to-close at tick 0

# ─── Model A : Optimistic ─────────────────────────────────────────────────────
MA_FLIP_PROB      = 0.35
MA_FLIP_WINDOW    = (60, 220)
MA_NOISE_SIGMA    = 0.008
MA_DRIFT_STRENGTH = 0.004
MA_OUTCOME_PRICE  = 0.85
MA_LOSER_PRICE    = 0.15
MA_OPEN_SPREAD    = 0.04

def _gen_optimistic() -> Tuple[List[float], List[float], str]:
    does_flip  = random.random() < MA_FLIP_PROB
    if does_flip:
        flip_tick    = random.randint(*MA_FLIP_WINDOW)
        first_w      = 'UP' if random.random() < 0.5 else 'DOWN'
        second_w     = 'DOWN' if first_w == 'UP' else 'UP'
        final        = second_w
    else:
        flip_tick    = N_TICKS + 1
        final        = 'UP' if random.random() < 0.5 else 'DOWN'
        first_w      = final; second_w = final

    if first_w == 'UP':
        up0 = 0.50 + MA_OPEN_SPREAD/2 + random.gauss(0, 0.01)
        dn0 = 0.50 - MA_OPEN_SPREAD/2 + random.gauss(0, 0.01)
    else:
        up0 = 0.50 - MA_OPEN_SPREAD/2 + random.gauss(0, 0.01)
        dn0 = 0.50 + MA_OPEN_SPREAD/2 + random.gauss(0, 0.01)

    ups = [max(0.05, min(0.95, up0))]
    dns = [max(0.05, min(0.95, dn0))]

    for t in range(1, N_TICKS):
        w = first_w if t < flip_tick else second_w
        ds = MA_DRIFT_STRENGTH * (0.5 + t / N_TICKS)
        up_new = ups[-1] + ds*(MA_OUTCOME_PRICE if w=='UP' else MA_LOSER_PRICE - ups[-1]) + random.gauss(0, MA_NOISE_SIGMA)
        dn_new = dns[-1] + ds*(MA_OUTCOME_PRICE if w=='DOWN' else MA_LOSER_PRICE - dns[-1]) + random.gauss(0, MA_NOISE_SIGMA)
        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.15:
            c = (mid - 0.5) * 0.3; up_new -= c; dn_new -= c
        ups.append(max(0.02, min(0.98, up_new)))
        dns.append(max(0.02, min(0.98, dn_new)))
    return ups, dns, final


# ─── Model B : Polymarket-realistic ──────────────────────────────────────────
MB_FLIP_PROB        = 0.50
MB_FLIP_WINDOW      = (40, 240)
MB_NOISE_SIGMA      = 0.012
MB_DRIFT_STRENGTH   = 0.0005
MB_FINAL_DRIFT      = 0.025
MB_OUTCOME_PRICE    = 0.88
MB_LOSER_PRICE      = 0.12
MB_OPEN_SPREAD      = 0.01
MB_JUMP_PROB        = 0.025
MB_JUMP_SIZE_MIN    = 0.06
MB_JUMP_SIZE_MAX    = 0.18
MB_RESOLUTION_START = 0.80

def _gen_polymarket() -> Tuple[List[float], List[float], str]:
    does_flip = random.random() < MB_FLIP_PROB
    if does_flip:
        flip_tick = random.randint(*MB_FLIP_WINDOW)
        first_w   = 'UP' if random.random() < 0.5 else 'DOWN'
        second_w  = 'DOWN' if first_w == 'UP' else 'UP'
        final     = second_w
    else:
        flip_tick = N_TICKS + 1
        final     = 'UP' if random.random() < 0.5 else 'DOWN'
        first_w   = final; second_w = final

    up0 = 0.50 + MB_OPEN_SPREAD/2 + random.gauss(0, 0.005)
    dn0 = 0.50 - MB_OPEN_SPREAD/2 + random.gauss(0, 0.005)
    ups = [max(0.05, min(0.95, up0))]
    dns = [max(0.05, min(0.95, dn0))]

    for t in range(1, N_TICKS):
        w        = first_w if t < flip_tick else second_w
        progress = t / N_TICKS

        if progress >= MB_RESOLUTION_START:
            ds = MB_FINAL_DRIFT * ((progress - MB_RESOLUTION_START) / (1 - MB_RESOLUTION_START))
        else:
            ds = MB_DRIFT_STRENGTH

        up_new = ups[-1] + ds*(MB_OUTCOME_PRICE if w=='UP' else MB_LOSER_PRICE - ups[-1]) + random.gauss(0, MB_NOISE_SIGMA)
        dn_new = dns[-1] + ds*(MB_OUTCOME_PRICE if w=='DOWN' else MB_LOSER_PRICE - dns[-1]) + random.gauss(0, MB_NOISE_SIGMA)

        if random.random() < MB_JUMP_PROB:
            jump = random.uniform(MB_JUMP_SIZE_MIN, MB_JUMP_SIZE_MAX)
            if w == 'UP': up_new += jump; dn_new -= jump
            else:         dn_new += jump; up_new -= jump

        mid = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.12:
            c = (mid - 0.5) * 0.25; up_new -= c; dn_new -= c

        ups.append(max(0.02, min(0.98, up_new)))
        dns.append(max(0.02, min(0.98, dn_new)))
    return ups, dns, final


# ─── Run one market through OppShotStrategy ───────────────────────────────────
def run_market(ups: List[float], dns: List[float], outcome: str) -> dict:
    cash_ref = {'balance': STARTING_BALANCE}
    strat = OppShotStrategy(
        cash_ref=cash_ref,
        market_budget=MARKET_BUDGET,
        starting_balance=STARTING_BALANCE,
    )

    with contextlib.redirect_stdout(io.StringIO()):
        for t, (up, dn) in enumerate(zip(ups, dns)):
            ttc = TTC_START - t
            strat.check_and_trade(
                up_price=up, down_price=dn,
                timestamp=str(t),
                time_to_close=ttc,
                up_bid=round(max(0.01, up - 0.005), 4),
                down_bid=round(max(0.01, dn - 0.005), 4),
            )
        pnl = strat.resolve_market(outcome)

    n_up    = len(strat._buys['UP'])
    n_dn    = len(strat._buys['DOWN'])
    n_pairs = min(n_up, n_dn)

    return {
        'pnl':          pnl,
        'spent':        strat.total_spent,
        'n_up':         n_up,
        'n_dn':         n_dn,
        'n_pairs':      n_pairs,
        'n_trades':     strat.trade_count,
        'outcome':      outcome,
    }


# ─── Stats helper ─────────────────────────────────────────────────────────────
def summarise(label: str, results: List[dict]):
    pnls    = [r['pnl']   for r in results]
    spents  = [r['spent'] for r in results]
    traded  = [r for r in results if r['spent'] > 0.01]
    traded_pnls  = [r['pnl'] for r in traded]

    wins    = sum(1 for p in pnls if p > 0)
    losses  = sum(1 for p in pnls if p < 0)
    zeros   = sum(1 for p in pnls if p == 0)
    no_trade = len(results) - len(traded)

    if not pnls:
        print(f"  {label}: no results"); return

    print(f"\n{'═'*60}")
    print(f"  {label}  ({len(results):,} markets)")
    print(f"{'─'*60}")
    print(f"  No trade (flat market):  {no_trade:>6,}  ({100*no_trade/len(results):.1f}%)")
    print(f"  Traded markets:          {len(traded):>6,}  ({100*len(traded)/len(results):.1f}%)")
    if traded:
        print(f"  Win  (PnL > 0):          {wins:>6,}  ({100*wins/len(results):.1f}%)")
        print(f"  Loss (PnL < 0):          {losses:>6,}  ({100*losses/len(results):.1f}%)")
        print(f"{'─'*60}")
        print(f"  Avg PnL (all):           {statistics.mean(pnls):>+8.2f}")
        print(f"  Avg PnL (traded only):   {statistics.mean(traded_pnls):>+8.2f}")
        print(f"  Median PnL:              {statistics.median(pnls):>+8.2f}")
        print(f"  Stdev PnL:               {statistics.stdev(pnls):>8.2f}")
        print(f"  Best market:             {max(pnls):>+8.2f}")
        print(f"  Worst market:            {min(pnls):>+8.2f}")
        print(f"  Total PnL (sum):         {sum(pnls):>+10.2f}")
        print(f"  Avg spent / market:      {statistics.mean(spents):>8.2f}")
        print(f"  Avg trades / market:     {statistics.mean(r['n_trades'] for r in results):>8.1f}")
        print(f"  Avg pairs / market:      {statistics.mean(r['n_pairs'] for r in results):>8.1f}")
        print(f"{'─'*60}")

        # PnL bucket distribution
        buckets = [(-999,-10),(-10,-5),(-5,-1),(-1,0),(0,1),(1,5),(5,10),(10,999)]
        labels  = ['< -10','−10→−5','−5→−1','−1→0','0→1','1→5','5→10','> +10']
        print("  PnL distribution:")
        for (lo, hi), lbl in zip(buckets, labels):
            n = sum(1 for p in pnls if lo <= p < hi)
            bar = '█' * int(40 * n / len(pnls))
            print(f"    {lbl:>8}: {n:>5,} {bar}")


# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"OppShot Monte Carlo  —  {N_MARKETS:,} markets × 2 models")
    print(f"Budget ${MARKET_BUDGET:.0f}/market  |  LEAD≥{OppShotStrategy.LEAD_THRESHOLD}  REBAL_CEIL≤{OppShotStrategy.REBALANCE_CEIL}  MAX_RUNG=${OppShotStrategy.MAX_RUNG}")

    res_a, res_b = [], []

    for i in range(N_MARKETS):
        if (i+1) % 500 == 0:
            print(f"  ... {i+1}/{N_MARKETS}", flush=True)
        ups_a, dns_a, out_a = _gen_optimistic()
        res_a.append(run_market(ups_a, dns_a, out_a))

        ups_b, dns_b, out_b = _gen_polymarket()
        res_b.append(run_market(ups_b, dns_b, out_b))

    summarise("MODEL A — Optimistic", res_a)
    summarise("MODEL B — Polymarket-realistic", res_b)

    # Combined
    all_res = res_a + res_b
    summarise("COMBINED (A + B)", all_res)

    print()
