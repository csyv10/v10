"""
mc_test_improvements.py
════════════════════════════════════════════════════════════════════════════
Quick A/B test: baseline params vs proposed improvements (1 000 markets).

Proposed changes vs current:
  FLIP_TRIGGER  0.50  → 0.45   (detect flip earlier)
  FLIP_CONFIRM  3     → 2      (faster confirmation)
  MAX_RUNGS     10    → 5      (less capital locked in ladder)
  RUNG_STEP     0.025 → 0.04   (fewer rungs per rally)
  + active sell losing-side rungs at flip detection (not hold_to_res)
"""

import random, statistics
from typing import List, Optional

random.seed(42)

# ─── Market parameters (shared) ───────────────────────────────────────────────
N_MARKETS      = 1_000
N_TICKS        = 300
FLIP_PROB      = 0.35
FLIP_WINDOW    = (60, 220)
NOISE_SIGMA    = 0.008
DRIFT_STRENGTH = 0.003
OUTCOME_PRICE  = 0.85
LOSER_PRICE    = 0.15
OPEN_SPREAD    = 0.04

# ─── Shared strategy parameters ───────────────────────────────────────────────
ENTRY_MIN       = 0.28
ENTRY_MAX       = 0.60
RUNG_SPREAD     = 0.050
RUNG_USD        = 2.00
MAX_RUNG_PRICE  = 0.92
RECOVERY_BUDGET = 3.00
RECOVERY_TARGET = 2.00
MAX_SIDE_COST   = 12.0
LOSS_CAP        = 3.00
MIN_TRADE       = 1.00
EXIT_TTC        = 50
EXIT_MIN_BID    = 0.28
MIN_RR          = 1.5
FLIP_BUDGET     = 10.00
VOL_THRESHOLD      = 0.06
VOL_MAX_RUNGS      = 4
VOL_SL_MULT        = 1.5
VOL_MOMENTUM_TICKS = 3
VOL_EXIT_EXTRA     = 30
DETECT_WINDOW      = 30   # fixed for this test


# ═══════════════════════════════════════════════════════════════════════════════
# Market generator
# ═══════════════════════════════════════════════════════════════════════════════

def generate_market():
    does_flip  = random.random() < FLIP_PROB
    flip_tick  = random.randint(*FLIP_WINDOW) if does_flip else N_TICKS + 1
    first_w    = 'UP' if random.random() < 0.5 else 'DOWN'
    second_w   = 'DOWN' if first_w == 'UP' else 'UP'
    final_out  = second_w if does_flip else first_w

    up0 = 0.50 + (OPEN_SPREAD/2 if first_w == 'UP'   else -OPEN_SPREAD/2) + random.gauss(0, 0.01)
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
        mid    = (up_new + dn_new) / 2
        if abs(mid - 0.5) > 0.15:
            c = (mid - 0.5) * 0.3; up_new -= c; dn_new -= c
        up_prices.append(max(0.02, min(0.98, up_new)))
        dn_prices.append(max(0.02, min(0.98, dn_new)))

    return up_prices, dn_prices, final_out


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
# Simulation core — parameterised
# ═══════════════════════════════════════════════════════════════════════════════

def simulate(up_prices, dn_prices, outcome,
             flip_trigger, flip_confirm, max_rungs, rung_step,
             active_sell_losers=False) -> float:

    pos     = {'UP': _Pos(), 'DOWN': _Pos()}
    rungs:  List[dict] = []
    realised      = 0.0
    phase         = 'observe'
    ladder_side   = None
    flip_ticks    = 0
    flip_bud      = FLIP_BUDGET
    last_rung_px  = 0.0
    rec_side:     Optional[str] = None
    rec_qty:      float         = 0.0
    rec_target:   float         = 0.0
    is_volatile:  bool          = False
    vol_prices:   List[float]   = []
    momentum:     int           = 0
    prev_ls_px:   float         = 0.0

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
    def sl_dist():
        return round((RUNG_SPREAD / MIN_RR) * (VOL_SL_MULT if is_volatile else 1.0), 4)
    def max_rungs_eff():
        return VOL_MAX_RUNGS if is_volatile else max_rungs
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
        nonlocal flip_bud, rec_side, rec_qty, rec_target, ladder_side, last_rung_px
        nonlocal flip_ticks, phase

        if active_sell_losers:
            # Sell losing-side rungs at current bid immediately instead of
            # locking them with hold_to_res — cuts dead-weight exposure
            bid_ls = max(0.01, other_price - 0.005)  # other_price proxy for losing side bid
            to_sell = [r for r in rungs if r['side'] == ls]
            for r in to_sell:
                do_sell(ls, bid_ls, r['qty'])
        else:
            for r in rungs:
                if r['side'] == ls and not r.get('hold_to_res'):
                    r['hold_to_res'] = True

        oc       = open_cost(ls)
        needed   = oc + RECOVERY_TARGET
        budget   = min(RECOVERY_BUDGET, flip_bud, avail_now,
                       MAX_SIDE_COST - pos[other].cost)
        if budget < MIN_TRADE or other_price <= 0.01: return 0.0
        qty = do_buy(other, other_price, budget)
        if not qty: return 0.0
        spent        = qty * other_price
        flip_bud    -= spent
        sell_tgt     = min(other_price + needed / qty, MAX_RUNG_PRICE)
        rec_side     = other; rec_qty = qty; rec_target = sell_tgt
        ladder_side  = other; last_rung_px = other_price
        phase        = 'flip_recover'
        flip_ticks   = 0
        return spent

    def open_cost(side): return sum(r['cost'] for r in rungs if r['side'] == side)

    for t, (up, dn) in enumerate(zip(up_prices, dn_prices)):
        ttc     = N_TICKS - t
        bid     = {'UP': max(0.01, up-0.005), 'DOWN': max(0.01, dn-0.005)}
        px      = {'UP': up, 'DOWN': dn}
        leading = 'UP' if up >= dn else 'DOWN'

        if t < DETECT_WINDOW:
            lead_px = up if up >= dn else dn
            vol_prices.append(lead_px)
        if t == DETECT_WINDOW - 1:
            is_volatile = (max(vol_prices) - min(vol_prices)) > VOL_THRESHOLD
            if phase == 'observe':
                phase = 'scout'

        if ttc < exit_ttc_eff():
            sold = [r for r in rungs
                    if not r.get('hold_to_res') and bid[r['side']] >= EXIT_MIN_BID]
            for r in sold: do_sell(r['side'], bid[r['side']], r['qty']); rungs.remove(r)

        if ttc < 10: continue

        sold = []
        for r in rungs:
            if r.get('hold_to_res'): continue
            s = r['side']; cp = px[s]
            if cp >= r['sell_target'] or cp <= r['stop_price']:
                do_sell(s, cp, r['qty']); sold.append(r)
        for r in sold: rungs.remove(r)

        if rec_side and rec_qty > 0.01 and px[rec_side] >= rec_target:
            do_sell(rec_side, px[rec_side], pos[rec_side].qty)
            rec_qty = 0.0; rec_side = None
            if ladder_side: phase = 'ladder'

        avail = available()

        if phase == 'observe':
            continue

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

        if phase == 'ladder':
            ls = ladder_side; ls_price = px[ls]
            other = 'DOWN' if ls == 'UP' else 'UP'

            if ls_price < flip_trigger and leading != ls: flip_ticks += 1
            else:                                          flip_ticks = 0

            if flip_ticks >= flip_confirm and flip_bud >= MIN_TRADE:
                try_flip(ls, other, px[other], avail)

            if phase == 'ladder':
                oc = open_count(ls); ok = momentum_ok(ls_price)
                if (ls_price >= last_rung_px + rung_step
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

        elif phase == 'flip_recover':
            ls = ladder_side; ls_price = px[ls]
            other = 'DOWN' if ls == 'UP' else 'UP'

            if ls_price < flip_trigger and leading != ls: flip_ticks += 1
            else:                                          flip_ticks = 0

            if flip_ticks >= flip_confirm and flip_bud >= MIN_TRADE:
                try_flip(ls, other, px[other], avail)
                ls = ladder_side; ls_price = px[ls]

            if phase in ('flip_recover', 'ladder'):
                oc = open_count(ls); ok = momentum_ok(ls_price)
                if (ls_price >= last_rung_px + rung_step
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

    payout    = pos[outcome].qty
    realised += payout - (pos['UP'].cost + pos['DOWN'].cost)
    return realised


# ═══════════════════════════════════════════════════════════════════════════════
# Stats helper
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
    return dict(avg=avg, std=std, wr=wr, rr=rr, p5=p5, p95=p95,
                avg_win=avg_win, avg_loss=avg_loss)


# ═══════════════════════════════════════════════════════════════════════════════
# A/B variants
# ═══════════════════════════════════════════════════════════════════════════════

VARIANTS = [
    dict(label='Baseline (current)',
         flip_trigger=0.50, flip_confirm=3, max_rungs=10, rung_step=0.025, active_sell=False),
    dict(label='Earlier trigger (0.45)',
         flip_trigger=0.45, flip_confirm=3, max_rungs=10, rung_step=0.025, active_sell=False),
    dict(label='Faster confirm (2 ticks)',
         flip_trigger=0.50, flip_confirm=2, max_rungs=10, rung_step=0.025, active_sell=False),
    dict(label='Fewer rungs (max 5)',
         flip_trigger=0.50, flip_confirm=3, max_rungs=5,  rung_step=0.025, active_sell=False),
    dict(label='Wider rung step (0.04)',
         flip_trigger=0.50, flip_confirm=3, max_rungs=10, rung_step=0.040, active_sell=False),
    dict(label='Active sell losers',
         flip_trigger=0.50, flip_confirm=3, max_rungs=10, rung_step=0.025, active_sell=True),
    dict(label='ALL improvements',
         flip_trigger=0.45, flip_confirm=2, max_rungs=5,  rung_step=0.040, active_sell=True),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Run
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print(f'\nMC A/B — {N_MARKETS:,} markets  (seed=42)\n')
    print(f'{"Variant":<30}  {"Avg PnL":>8}  {"std":>6}  {"WR%":>6}  '
          f'{"R:R":>5}  {"AvgW":>6}  {"AvgL":>7}  {"P5":>7}  {"P95":>7}')
    print('─' * 100)

    print(f'Generating {N_MARKETS:,} markets...', flush=True)
    markets = [generate_market() for _ in range(N_MARKETS)]
    print(f'Done. Running {len(VARIANTS)} variants...\n')

    baseline_avg = None
    for v in VARIANTS:
        pnls = [simulate(up, dn, out,
                         flip_trigger=v['flip_trigger'],
                         flip_confirm=v['flip_confirm'],
                         max_rungs=v['max_rungs'],
                         rung_step=v['rung_step'],
                         active_sell_losers=v['active_sell'])
                for up, dn, out in markets]
        s = stats(pnls)
        if baseline_avg is None:
            baseline_avg = s['avg']
        delta = s['avg'] - baseline_avg
        flag  = f'  Δ{delta:+.4f}' if delta != 0 else '  (baseline)'
        print(f'{v["label"]:<30}  ${s["avg"]:>+7.4f}  {s["std"]:>6.4f}  '
              f'{s["wr"]:>5.1f}%  {s["rr"]:>5.2f}x  '
              f'${s["avg_win"]:>+5.3f}  ${s["avg_loss"]:>+6.3f}  '
              f'${s["p5"]:>+6.3f}  ${s["p95"]:>+6.3f}'
              f'{flag}')

    print()
