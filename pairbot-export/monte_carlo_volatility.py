"""
monte_carlo_volatility.py
════════════════════════════════════════════════════════════════════════════
Monte Carlo: 10 000 markets — adaptive volatility regime

BASELINE: current live config

ADAPTIVE: detects volatile markets during first VOL_DETECT_WINDOW ticks.
  If range (max-min) > VOL_THRESHOLD → "volatile regime":
    1. MAX_RUNGS reduced to VOL_MAX_RUNGS (4)
    2. SL distance widened by VOL_SL_MULT (×1.5)
    3. New rung requires momentum: last 3 ticks all rising
    4. EXIT_TTC extended by VOL_EXIT_EXTRA seconds (30s earlier)

Also reports separately on:
  - Non-volatile markets: to see if baseline is better there
  - Volatile markets only: to see the improvement where it matters
"""

import random
import statistics
from typing import List, Tuple

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
ENTRY_MIN      = 0.28
ENTRY_MAX      = 0.60
RUNG_SPREAD    = 0.050
RUNG_STEP      = 0.025
RUNG_USD       = 2.00
MAX_RUNG_PRICE = 0.92
MAX_RUNGS      = 10
FLIP_TRIGGER   = 0.50
FLIP_CONFIRM   = 3
RECOVERY_BUDGET = 3.00
RECOVERY_TARGET = 2.00
MAX_SIDE_COST  = 12.0
LOSS_CAP       = 3.00
MIN_TRADE      = 1.00
EXIT_TTC       = 50
EXIT_MIN_BID   = 0.28
MIN_RR         = 1.5

# ─── Adaptive volatility parameters ───────────────────────────────────────────
VOL_DETECT_WINDOW = 30     # ticks to observe before deciding regime
VOL_THRESHOLD     = 0.06   # range (max-min) above this = volatile
VOL_MAX_RUNGS     = 4      # reduced max rungs in volatile regime
VOL_SL_MULT       = 1.5    # wider stop-loss multiplier in volatile regime
VOL_MOMENTUM_TICKS = 3     # require N consecutive up-ticks before new rung
VOL_EXIT_EXTRA    = 30     # extend exit window by this many seconds (earlier exit)


# ═══════════════════════════════════════════════════════════════════════════════
# Price path generator
# ═══════════════════════════════════════════════════════════════════════════════

def generate_market() -> Tuple[List[float], List[float], str, bool]:
    """Returns (up_prices, dn_prices, outcome, is_volatile)."""
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

    # Determine volatility: range of leading side in first VOL_DETECT_WINDOW ticks
    window_up = up_prices[:VOL_DETECT_WINDOW]
    window_dn = dn_prices[:VOL_DETECT_WINDOW]
    # Use the side that leads at tick 0
    leading_window = window_up if up_prices[0] >= dn_prices[0] else window_dn
    vol_range = max(leading_window) - min(leading_window)
    is_volatile = vol_range > VOL_THRESHOLD

    return up_prices, dn_prices, final_outcome, is_volatile


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
# Simulate one market
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_market(up_prices, dn_prices, outcome, is_volatile,
                    adaptive: bool) -> float:
    # ── regime-dependent params ──────────────────────────────────────────────
    if adaptive and is_volatile:
        max_rungs_eff = VOL_MAX_RUNGS
        sl_mult       = VOL_SL_MULT
        exit_ttc_eff  = EXIT_TTC + VOL_EXIT_EXTRA
        use_momentum  = True
    else:
        max_rungs_eff = MAX_RUNGS
        sl_mult       = 1.0
        exit_ttc_eff  = EXIT_TTC
        use_momentum  = False

    sl_dist = round((RUNG_SPREAD / MIN_RR) * sl_mult, 4)

    pos      = {'UP': _Pos(), 'DOWN': _Pos()}
    rungs    = []
    realised = 0.0
    cash_out = 0.0

    phase        = 'scout'
    ladder_side  = None
    flip_ticks   = 0
    flipped_once = False
    last_rung_px = 0.0

    rec_side   = None
    rec_qty    = 0.0
    rec_target = 0.0

    price_hist = {'UP': [], 'DOWN': []}

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
        if qty < 0.001: return
        cb = pos[side].remove(qty)
        realised += qty * price - cb

    def open_rung_count(side):
        return sum(1 for r in rungs if r['side'] == side)

    def open_rung_cost(side):
        return sum(r['cost'] for r in rungs if r['side'] == side)

    def momentum_ok(side) -> bool:
        """Require last VOL_MOMENTUM_TICKS all rising on active side."""
        if not use_momentum: return True
        hist = price_hist[side]
        if len(hist) < VOL_MOMENTUM_TICKS + 1: return True
        recent = hist[-(VOL_MOMENTUM_TICKS + 1):]
        return all(recent[i] > recent[i-1] for i in range(1, len(recent)))

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        bid_up  = max(0.01, up - 0.005)
        bid_dn  = max(0.01, dn - 0.005)
        bid     = {'UP': bid_up, 'DOWN': bid_dn}
        px      = {'UP': up, 'DOWN': dn}
        leading = 'UP' if up >= dn else 'DOWN'

        price_hist['UP'].append(up)
        price_hist['DOWN'].append(dn)

        # ── EXIT WINDOW (regime-aware) ───────────────────────────────────────
        if ttc < exit_ttc_eff:
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
        if rec_side and rec_qty > 0.01:
            if px[rec_side] >= rec_target:
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

        # ── LADDER ───────────────────────────────────────────────────────────
        if phase == 'ladder':
            ls = ladder_side; ls_price = px[ls]
            other = 'DOWN' if ls == 'UP' else 'UP'

            if ls_price < FLIP_TRIGGER and leading != ls:
                flip_ticks += 1
            else:
                flip_ticks = 0

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
                        sell_tgt    = min(other_price + net_needed / qty, MAX_RUNG_PRICE)
                        rec_side    = other; rec_qty = qty; rec_target = sell_tgt
                        phase       = 'flip_recover'
                        ladder_side = other; last_rung_px = other_price

            if phase == 'ladder':
                oc = open_rung_count(ls)
                if (ls_price >= last_rung_px + RUNG_STEP
                        and ls_price <= MAX_RUNG_PRICE
                        and oc < max_rungs_eff          # ← regime-aware cap
                        and available() >= RUNG_USD
                        and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST
                        and momentum_ok(ls)):            # ← momentum gate
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

        elif phase == 'flip_recover':
            ls = ladder_side; ls_price = px[ls]
            oc = open_rung_count(ls)
            if (ls_price >= last_rung_px + RUNG_STEP
                    and ls_price <= MAX_RUNG_PRICE
                    and oc < max_rungs_eff
                    and available() >= RUNG_USD
                    and pos[ls].cost + RUNG_USD <= MAX_SIDE_COST
                    and momentum_ok(ls)):
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
# Run
# ═══════════════════════════════════════════════════════════════════════════════

def stats_block(pnls, label, indent='  '):
    wins     = sum(1 for p in pnls if p > 0)
    losses   = sum(1 for p in pnls if p < 0)
    total    = sum(pnls)
    avg      = statistics.mean(pnls)
    std      = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    wr       = wins / len(pnls) * 100
    avg_win  = statistics.mean(p for p in pnls if p > 0) if wins  else 0
    avg_loss = statistics.mean(p for p in pnls if p < 0) if losses else 0
    rr       = abs(avg_win / avg_loss) if avg_loss else float('inf')
    sorted_p = sorted(pnls)
    p5       = sorted_p[int(0.05 * len(sorted_p))]
    p95      = sorted_p[int(0.95 * len(sorted_p))]
    worst    = min(pnls)
    print(f"{indent}{'─'*52}")
    print(f"{indent}{label}  (n={len(pnls):,})")
    print(f"{indent}{'─'*52}")
    print(f"{indent}Win / Loss        : {wins:,} / {losses:,}   WR={wr:.1f}%")
    print(f"{indent}Total PnL         : ${total:+,.2f}")
    print(f"{indent}Avg PnL / market  : ${avg:+.4f}")
    print(f"{indent}Std dev           : ${std:.4f}")
    print(f"{indent}Avg win / avg loss: ${avg_win:+.4f} / ${avg_loss:+.4f}   R:R={rr:.2f}x")
    print(f"{indent}P5 / P95          : ${p5:+.4f} / ${p95:+.4f}")
    print(f"{indent}Worst market      : ${worst:+.4f}")
    return avg


def run():
    print(f'Generating {N_MARKETS:,} market paths...')
    markets = [generate_market() for _ in range(N_MARKETS)]

    n_volatile = sum(1 for _, _, _, v in markets if v)
    n_calm     = N_MARKETS - n_volatile
    print(f'Volatile markets  : {n_volatile:,} ({n_volatile/N_MARKETS*100:.1f}%)')
    print(f'Calm markets      : {n_calm:,} ({n_calm/N_MARKETS*100:.1f}%)')
    print(f'Volatility threshold: range > {VOL_THRESHOLD} in first {VOL_DETECT_WINDOW} ticks\n')

    # ── simulate both variants ───────────────────────────────────────────────
    base_pnls     = []
    adaptive_pnls = []
    for up, dn, out, vol in markets:
        base_pnls.append(simulate_market(up, dn, out, vol, adaptive=False))
        adaptive_pnls.append(simulate_market(up, dn, out, vol, adaptive=True))

    # ── overall results ──────────────────────────────────────────────────────
    print(f"{'═'*56}")
    print(f"  OVERALL RESULTS  ({N_MARKETS:,} markets)")
    print(f"{'═'*56}")
    avg_base = stats_block(base_pnls,     'BASELINE (static)')
    avg_adap = stats_block(adaptive_pnls, 'ADAPTIVE (volatile-aware)')

    delta = avg_adap - avg_base
    verdict = '✅ ADAPTIVE IS BETTER' if delta > 0.001 else ('❌ ADAPTIVE IS WORSE' if delta < -0.001 else '➖ NO MEANINGFUL DIFFERENCE')
    print(f"\n  Δ avg/market: {delta:+.4f}   {verdict}")
    print(f"  Δ total:      ${(avg_adap - avg_base) * N_MARKETS:+,.2f} over {N_MARKETS:,} markets")

    # ── split: volatile vs calm ──────────────────────────────────────────────
    base_vol   = [b for b, (_, _, _, v) in zip(base_pnls,     markets) if v]
    adap_vol   = [a for a, (_, _, _, v) in zip(adaptive_pnls, markets) if v]
    base_calm  = [b for b, (_, _, _, v) in zip(base_pnls,     markets) if not v]
    adap_calm  = [a for a, (_, _, _, v) in zip(adaptive_pnls, markets) if not v]

    print(f"\n{'═'*56}")
    print(f"  VOLATILE MARKETS ONLY  (n={n_volatile:,})")
    print(f"{'═'*56}")
    avg_bv = stats_block(base_vol,  'BASELINE  — volatile')
    avg_av = stats_block(adap_vol,  'ADAPTIVE  — volatile')
    dv = avg_av - avg_bv
    verd_v = '✅ BETTER' if dv > 0.001 else ('❌ WORSE' if dv < -0.001 else '➖ NEUTRAL')
    print(f"\n  Δ avg/market (volatile): {dv:+.4f}   {verd_v}")
    print(f"  Δ total (volatile):      ${dv * n_volatile:+,.2f}")

    print(f"\n{'═'*56}")
    print(f"  CALM MARKETS ONLY  (n={n_calm:,})")
    print(f"{'═'*56}")
    avg_bc = stats_block(base_calm, 'BASELINE  — calm')
    avg_ac = stats_block(adap_calm, 'ADAPTIVE  — calm')
    dc = avg_ac - avg_bc
    verd_c = '✅ BETTER' if dc > 0.001 else ('❌ WORSE' if dc < -0.001 else '➖ NEUTRAL')
    print(f"\n  Δ avg/market (calm): {dc:+.4f}   {verd_c}")

    # ── bar chart ────────────────────────────────────────────────────────────
    print(f"\n{'═'*56}")
    print(f"  SUMMARY")
    print(f"{'═'*56}")
    rows = [
        ('Overall — baseline',          avg_base),
        ('Overall — adaptive',          avg_adap),
        ('Volatile only — baseline',    avg_bv),
        ('Volatile only — adaptive',    avg_av),
        ('Calm only — baseline',        avg_bc),
        ('Calm only — adaptive',        avg_ac),
    ]
    max_val = max(abs(v) for _, v in rows)
    bw = 30
    for lbl, val in rows:
        bar = ('█' if val >= 0 else '▒') * int(abs(val) / max_val * bw)
        print(f"  {lbl:<35}  ${val:>+7.4f}  {bar}")
    print()


if __name__ == '__main__':
    print(f'\nMonte Carlo: {N_MARKETS:,} markets — adaptive volatility regime')
    print(f'VOL_THRESHOLD={VOL_THRESHOLD}  detect_window={VOL_DETECT_WINDOW}ticks')
    print(f'Volatile regime: MAX_RUNGS={VOL_MAX_RUNGS}  SL×{VOL_SL_MULT}  '
          f'momentum={VOL_MOMENTUM_TICKS}ticks  exit+{VOL_EXIT_EXTRA}s\n')
    run()
