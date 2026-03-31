"""
laddermate_strategy.py  —  LadderMate v1
══════════════════════════════════════════════════════════════════════════════

PHILOSOPHY
  A buy/sell ladder on the trending side, with automatic flip recovery.

NORMAL OPERATION  (phase: ladder)
  1. Enter the leading side on market open if its price is ≤ ENTRY_MAX (0.60).
  2. Place the first rung: buy RUNG_USD at current price, sell target = price + SPREAD.
  3. Each tick: if current price >= last_rung_price + RUNG_STEP, add another rung
     (buy at current price, sell target = current + SPREAD).
  4. Each tick: for every open rung whose sell_target is hit (price ≥ target), SELL
     those shares immediately and lock the rung profit.
  5. Continue placing rungs while price < MAX_RUNG_PRICE and budget allows.

FLIP DETECTION
  When the active side's price drops below FLIP_TRIGGER (0.50) AND the other
  side has led for FLIP_CONFIRM_TICKS consecutive ticks.

FLIP RECOVERY  (phase: flip_recover)
  On flip:
  1. Stop placing new rungs on old side (keep existing rungs — they may still
     recover partially or resolve at their price if market flips back).
  2. Calculate total open exposure: open_cost = sum of cost of unsold old rungs.
  3. Calculate recovery needed: net_needed = open_cost + RECOVERY_PROFIT_TARGET.
  4. Buy a recovery position on the new side (recovery_budget $ at current price).
     recovery_qty = recovery_budget / new_price
     flip_sell_target = new_price + net_needed / recovery_qty
  5. When new_side_price >= flip_sell_target → SELL full recovery position.
  6. After selling recovery, resume a fresh ladder on the new side.

RESOLUTION
  Any unsold shares are worth $1 each if the correct side wins, $0 otherwise.
  The strategy tracks all costs and proceeds so final PnL is exact.

PARAMETERS (tunable as class constants)
  ENTRY_MAX_PRICE   = 0.60   only enter if trending side price ≤ this
  ENTRY_MIN_PRICE   = 0.28   don't enter if price is too cheap (no room for spread)
  RUNG_SPREAD       = 0.020  sell_target = buy_price + RUNG_SPREAD
  RUNG_STEP         = 0.025  min price rise before next rung
  RUNG_USD          = 5.00   dollars per rung
  MAX_RUNG_PRICE    = 0.92   never place a rung above this
  MAX_RUNGS         = 12     max open rungs at any time
  FLIP_TRIGGER      = 0.50   active side must drop below this to flip
  FLIP_CONFIRM_TICKS = 3     consecutive ticks the other side must lead
  RECOVERY_PROFIT_TARGET = 5.00  desired net profit after flip recovery
  RECOVERY_BUDGET   = 30.00  max $ to spend on flip recovery buy
  MAX_SIDE_COST     = 120.0  hard spend cap per side per market
  MARKET_BUDGET     = 250.0  total budget per market
══════════════════════════════════════════════════════════════════════════════
"""

import time
from collections import deque
from typing import List, Tuple, Optional, Dict, Any

class _DummyPredictor:
    current_spot_price: Optional[float] = None
    market_open_price:  Optional[float] = None
    def update_spot_price(self, *a, **kw): pass
    def set_market_open_price(self, *a, **kw): pass
    def reset_for_new_market(self, *a, **kw): pass
    def predict(self): return None, 0.0, ''
    def record_market_outcome(self, *a, **kw): pass

class _Position:
    """Tracks shares and average cost for one side — used for UI display."""
    __slots__ = ('qty', 'cost', 'entry', 'signal', 'entry_time')

    def __init__(self):
        self.qty        = 0.0
        self.cost       = 0.0
        self.entry      = 0.0
        self.signal     = ''
        self.entry_time = 0.0

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty > 0.001 else 0.0

    def add(self, qty: float, price: float, signal: str = ''):
        self.cost  += qty * price
        self.qty   += qty
        self.entry  = self.avg
        self.signal = signal
        self.entry_time = time.time()

    def remove(self, qty: float) -> float:
        """Remove qty from position. Returns the cost-basis removed."""
        qty = min(qty, self.qty)
        if self.qty < 0.001:
            return 0.0
        avg = self.avg
        cost_removed   = avg * qty
        self.qty      -= qty
        self.cost     -= cost_removed
        if self.qty < 0.01:
            self.qty  = 0.0
            self.cost = 0.0
        return cost_removed

    def clear(self):
        self.qty = 0.0; self.cost = 0.0
        self.entry = 0.0; self.signal = ''

class LadderMateStrategy:
    """LadderMate — ladder buy/sell on trending side, with flip recovery."""

    STRATEGY_NAME = 'LadderMate_v1'

    # ── hard risk cap ─────────────────────────────────────────────────────
    LOSS_CAP = 8.00   # per-market stop (exit+restart on flip keeps losses small)

    # ── budget ──────────────────────────────────────────────────────────────
    MARKET_BUDGET  = 250.0
    MAX_SIDE_COST  = 20.0    # max spend per side; after flip old side is exited so new side gets full cap

    # ── entry gate ──────────────────────────────────────────────────────────
    ENTRY_MIN_PRICE = 0.65   # only enter when clear direction (avoid midrange volatility)
    ENTRY_MAX_PRICE = 0.75   # widened to capture more markets

    # ── rung parameters ─────────────────────────────────────────────────────
    RUNG_SPREAD     = 0.0835  # profit per share: sell_target = buy + SPREAD
    RUNG_STEP       = 0.025  # price must rise at least this much for next rung
    RUNG_USD        = 8.00   # dollars per rung
    MAX_RUNG_PRICE  = 0.92   # never start a rung above this
    MAX_RUNGS       = 10     # max open (unsold) rungs on the active side

    # ── flip detection ──────────────────────────────────────────────────────
    FLIP_TRIGGER        = 0.50   # active side drops below this → flip candidate
    FLIP_CONFIRM_TICKS  = 2      # other side must lead for N ticks to confirm

    # ── flip strategy: exit + restart ────────────────────────────────────────
    # On confirmed flip: sell all old-side rungs at bid (accept the slippage),
    # then start a fresh ladder on the new side.  No recovery position is placed.
    # This keeps max drawdown bounded: worst case = spread × rungs open at flip time.
    RECOVERY_PROFIT_TARGET = 2.00   # kept for compatibility; not used in exit+restart
    RECOVERY_BUDGET        = 0.00   # disabled — no recovery buy placed
    FLIP_BUDGET            = 0.00   # disabled — no recovery buy placed

    # ── timing ──────────────────────────────────────────────────────────────
    WARMUP_SECS = 20   # trading freeze at market open (let opening volatility settle)
    HOLD_TTC    = 10   # freeze all new activity in last 10 s
    EXIT_TTC    = 50   # start active exit of open rungs in last 50 s
    EXIT_MIN_BID = 0.28  # only sell on exit if bid >= this
                         # (below this the market has decided against us —
                          #  holding to resolution has higher EV than selling)

    # ── risk / reward ──────────────────────────────────────────────────────────
    MAX_LOSS_PER_MARKET = 5.00  # stop trading if live PnL drops below -$X; force-sell all
    MIN_RR = 1.67  # SL_distance = RUNG_SPREAD / MIN_RR = 0.055 / 1.67 = 0.033
    # TP = +0.055, SL = -0.033
    SL_SETTLE_WINDOW_S = 7     # seconds after buy when wider SL applies
    SL_SETTLE_MULT     = 1.5   # SL distance multiplier during settlement window

    # ── adaptive volatility regime ────────────────────────────────────────────
    VOL_THRESHOLD      = 0.06  # range > this = volatile market (kept for reference)
    VOL_MAX_RUNGS      = 4     # max rungs in volatile regime (vs 10 normal)
    VOL_SL_MULT        = 1.5   # widen SL distance by this factor in volatile regime
    VOL_MOMENTUM_TICKS = 3     # require N consecutive up-ticks before new rung
    VOL_EXIT_EXTRA     = 30    # extend exit window by this many seconds in volatile regime

    # ── orderbook parameters ─────────────────────────────────────────────────

    # ── chain settlement ────────────────────────────────────────────────────
    SETTLE_SECS = 12   # min seconds after BUY before a rung can be SOLD

    # ── compound rung sizing ("house money") ──────────────────────────────
    COMPOUND_RUNG_ENABLED = False   # roll TP profit into next rung size
    COMPOUND_RUNG_MAX     = 15.00  # max bonus on top of RUNG_USD (cap at 2x)
                       # (CLOB needs update_balance_allowance + chain scan)

    # ── misc ────────────────────────────────────────────────────────────────
    MIN_TRADE = 1.00
    MIN_PRICE = 0.04
    MAX_PRICE = 0.96

    # ── legacy compat ────────────────────────────────────────────────────────
    TREND_BUY_SIZE        = 1.00
    ARB_BUY_SIZE          = 5.00
    FLIP_BUY_SIZE         = 1.00
    TREND_COOLDOWN_TICKS  = 1
    ARB_COOLDOWN_TICKS    = 1
    FLIP_COOLDOWN_TICKS   = 1
    # FLIP_CONFIRM_TICKS removed — defined at line 135, adaptive at runtime
    MAX_PAIR_COST         = 1.00
    PAIR_COST_CAP         = 1.00
    FLIP_RECOVERY_MAX_PRICE = 0.90
    MIN_BALANCE_RATIO     = 0.80
    MAX_BALANCE_PRICE     = 0.90
    REBALANCE_CHEAP       = 0.90
    REBALANCE_BUDGET      = 250.0
    MAX_ENTRY_PRICE       = 0.75
    MIN_SPREAD            = 0.00
    ARB_THRESHOLD         = 0.95
    ARB_USD_PER_SIDE      = 5.00
    PROFIT_GOAL           = 10.00
    LOSS_LIMIT            = -20.00

    # ════════════════════════════════════════════════════════════════════════
    def __init__(self, market_budget=None, starting_balance=1000.0, exec_sim=None, mirror_mode=False):
        self.mirror_mode = mirror_mode
        # Read MAX_LOSS_PER_MARKET from env if configured via UI
        import os as _os
        _env_loss = _os.environ.get('MAX_LOSS_PER_MARKET', '')
        if _env_loss:
            try:
                self.MAX_LOSS_PER_MARKET = float(_env_loss)
            except ValueError:
                pass
        self.market_budget    = market_budget or self.MARKET_BUDGET
        self.starting_balance = starting_balance
        self.cash_ref         = {'balance': starting_balance}

        self._pos: Dict[str, _Position] = {
            'UP': _Position(), 'DOWN': _Position()
        }

        self._tick              = 0
        self._market_start_ttc:  float = -1.0  # TTC on first tick (set once)
        self._flip_ticks   = 0    # consecutive ticks where non-active side leads
        self._flip_budget: float = self.FLIP_BUDGET  # remaining $ for recovery buys

        # ── phase: 'scout' → 'ladder' → 'flip_recover' ─────────────────────
        self._phase:       str           = 'scout'
        self._ladder_side: Optional[str] = None   # which side the ladder is on

        # ── open rungs: list of dicts ────────────────────────────────────────
        # Each rung: {side, buy_price, sell_target, qty, cost, hold_to_res}
        # hold_to_res=True: don't early-sell, let resolve at $1/$0 at expiry
        self._rungs: List[dict] = []
        # Buffer of recently-sold rungs (keyed by trade order); used by
        # reconcile_sell to restore a rung if the live SELL order failed.
        self._sold_rung_buffer: List[dict] = []

        # highest buy_price placed on the active side (to detect step threshold)
        self._last_rung_price: float = 0.0

        # ── flip recovery state ──────────────────────────────────────────────
        self._recovery_side:   Optional[str] = None
        self._recovery_qty:    float         = 0.0
        self._recovery_target: float         = 0.0  # price target to sell recovery
        self._recovery_cost:   float         = 0.0

        # ── accounting ───────────────────────────────────────────────────────
        self._market_rung_bonus = 0.0  # accumulated TP profit for compound sizing
        self.cash_out     = 0.0
        self.cash_in      = 0.0
        self.realised_pnl = 0.0
        self.trade_count  = 0
        self.trade_log: List[dict] = []

        self._loss_limit_hit     = False  # set when MAX_LOSS_PER_MARKET breached
        self.market_status       = 'open'
        self.current_mode        = 'scout'
        self.mode_reason         = ''
        self.resolution_outcome: Optional[str] = None
        self.final_pnl:          Optional[float] = None
        self.final_pnl_gross:    Optional[float] = None
        self.last_fees_paid:     float = 0.0
        self.payout:             float = 0.0

        self._ask: Dict[str, float] = {'UP': 0.50, 'DOWN': 0.50}
        self._bid: Dict[str, float] = {'UP': 0.495, 'DOWN': 0.495}

        # ── orderbook imbalance tracking ─────────────────────────────────────
        self._pending_sells: List[dict] = []  # rungs that must be sold (retry every tick)

        # ── chaos detection ──────────────────────────────────────────────────
        self._chaos_mode: bool = False          # True = sell all, sit still
        self._imb_flip_count: int = 0           # imbalance sign flips in window
        self._imb_prev_sign: int = 0            # +1, -1, or 0
        self._price_dir_flips: int = 0          # price direction changes in window
        self._prev_lead_price: float = 0.0      # previous tick's lead price
        self._prev_price_dir: int = 0           # +1 rising, -1 falling
        self._chaos_window: int = 0             # ticks since last window reset
        self._CHAOS_WINDOW_SIZE: int = 300      # ~6 seconds of ticks
        self._CHAOS_FLIP_THRESHOLD: int = 8     # imb flips in window to trigger
        self._CHAOS_PRICE_THRESHOLD: int = 12   # price dir changes in window to trigger

        # ── compat ───────────────────────────────────────────────────────────
        self._pair_locked   = False
        self._arb_locked    = False
        self._primary: Optional[str] = None
        self._flip_counter  = 0
        self._main_side: Optional[str] = None
        self._trend_score   = 0.0
        self._trend_side: Optional[str] = None
        self.trend_predictor = _DummyPredictor()
        self._spot_prediction: Optional[str] = None
        self._spot_confidence = 0.0
        self._spot_reason: str = ''

    # ════════════════════════════════════════════════════════════════════════
    # Properties
    # ════════════════════════════════════════════════════════════════════════
    @property
    def cash(self) -> float:
        return self.cash_ref['balance']
    @cash.setter
    def cash(self, v: float):
        self.cash_ref['balance'] = v

    @property
    def qty_up(self)    -> float: return self._pos['UP'].qty
    @property
    def qty_down(self)  -> float: return self._pos['DOWN'].qty
    @property
    def cost_up(self)   -> float: return self._pos['UP'].cost
    @property
    def cost_down(self) -> float: return self._pos['DOWN'].cost
    @property
    def avg_up(self)    -> float: return self._pos['UP'].avg
    @property
    def avg_down(self)  -> float: return self._pos['DOWN'].avg

    @property
    def pair_cost(self) -> float:
        # Not a pair strategy — return a UI-friendly value
        if self.qty_up > 0.001 and self.qty_down > 0.001:
            return self._pos['UP'].avg + self._pos['DOWN'].avg
        return 0.0

    @property
    def locked_profit(self) -> float:
        """Confirmed realized profit from sold rungs."""
        return self.realised_pnl

    @property
    def best_case_profit(self) -> float:
        """Realised + unrealized (open rungs hit all their targets)."""
        open_upside = sum(
            r['qty'] * self.RUNG_SPREAD for r in self._rungs
        )
        recovery_upside = 0.0
        if self._recovery_qty > 0 and self._recovery_side:
            ask = self._ask[self._recovery_side]
            recovery_upside = max(0.0,
                self._recovery_qty * (self._recovery_target - ask)
            )
        return self.realised_pnl + open_upside + recovery_upside

    @property
    def qty_ratio(self) -> float:
        u, d = self.qty_up, self.qty_down
        if u < 0.001 or d < 0.001:
            return 0.0
        return max(u, d) / min(u, d)

    @property
    def total_spent(self) -> float:
        return self._pos['UP'].cost + self._pos['DOWN'].cost

    # ════════════════════════════════════════════════════════════════════════
    # Helpers
    # ════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _book_imbalance(orderbook, min_depth=30.0):
        """Compute bid/ask imbalance from top-5 levels.
        Returns float in [-1.0, +1.0] or None if book too thin."""
        if not orderbook:
            return None
        def _vol(levels):
            v = 0.0
            for lvl in levels[:5]:
                if isinstance(lvl, dict):
                    v += float(lvl.get('size', 0) or lvl.get('quantity', 0))
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    v += float(lvl[1])
            return v
        bid_v = _vol(orderbook.get('bids', []))
        ask_v = _vol(orderbook.get('asks', []))
        total = bid_v + ask_v
        if total < min_depth:
            return None
        return (bid_v - ask_v) / total

    def calculate_locked_profit(self) -> float: return self.realised_pnl
    def calculate_total_fees(self)   -> float: return 0.0

    def _available(self) -> float:
        spent    = self._pos['UP'].cost + self._pos['DOWN'].cost
        budget_r = max(0.0, self.market_budget - spent)
        return min(max(0.0, self.cash), budget_r)

    def _max_buy_within_cap(self, side: str, price: float) -> float:
        """
        Max USD we can spend on `side` without pushing worst-case loss past LOSS_CAP.

        Buying side=UP for $usd:
          - pnl_if_UP  improves by usd*(1/price - 1)  (more UP shares at cost)
          - pnl_if_DOWN worsens by usd                (UP shares worthless if DOWN wins)
          => constraint: pnl_if_DOWN - usd >= -LOSS_CAP  =>  usd <= pnl_if_DOWN + LOSS_CAP

        Buying side=DOWN for $usd: symmetric — constraint on pnl_if_UP.
        """
        if side == 'UP':
            # Buying UP hurts worst-case when DOWN wins
            p_other = self._pnl_if('DOWN')
        else:
            # Buying DOWN hurts worst-case when UP wins
            p_other = self._pnl_if('UP')
        return max(0.0, p_other + self.LOSS_CAP)

    def _guaranteed_pnl(self) -> float:
        return self.realised_pnl  # settled profit only

    def _pnl_if(self, outcome: str) -> float:
        """Best-case if the given side resolves at $1 (unsold shares pay out)."""
        up = self._pos['UP']
        dn = self._pos['DOWN']
        total_cost = up.cost + dn.cost
        payout     = up.qty if outcome == 'UP' else dn.qty
        return payout - total_cost + self.realised_pnl

    def _open_rung_count(self, side: str) -> int:
        return sum(1 for r in self._rungs if r['side'] == side)

    def _open_rung_cost(self, side: str) -> float:
        return sum(r['cost'] for r in self._rungs if r['side'] == side)

    def _do_buy(self, side: str, price: float, usd: float, reason: str) \
            -> Optional[Tuple]:
        """Execute a buy: update accounting and return trade tuple."""
        # Hard loss cap: never let worst-case exceed LOSS_CAP
        cap_room = self._max_buy_within_cap(side, price)
        usd = min(usd, cap_room, self._available())
        if usd < self.MIN_TRADE:
            return None
        price = max(self.MIN_PRICE, min(self.MAX_PRICE, price))
        qty = usd / price
        # Polymarket min order = 5 shares.  If we can't buy at least 5 we'd
        # create an unsellable rung, so skip the trade entirely.
        if qty < 5.0:
            return None
        self.cash     -= usd
        self.cash_out += usd
        self.trade_count += 1
        self._pos[side].add(qty, price, reason)
        return ('BUY', side, price, qty)

    def _do_sell(self, side: str, price: float, qty: float, reason: str) \
            -> Optional[Tuple]:
        """Execute a sell: update accounting and return trade tuple."""
        qty = min(qty, self._pos[side].qty)
        if qty < 0.01:
            return None
        price      = max(self.MIN_PRICE, min(self.MAX_PRICE, price))
        proceeds   = qty * price
        cost_basis = self._pos[side].remove(qty)
        pnl        = proceeds - cost_basis

        self.cash         += proceeds
        self.cash_in      += proceeds
        self.realised_pnl += pnl
        self.trade_count  += 1
        return ('SELL', side, price, qty, pnl)

    def reconcile_buy(self, side: str,
                      intended_qty: float, intended_price: float,
                      actual_qty: float,   actual_price: float):
        """
        Correct paper position AND _rungs list to match actual live BUY fill.
        Called immediately after live executor returns the real fill result.
        intended_* = what check_and_trade() planned (already applied to _pos).
        actual_*   = what the exchange actually filled (0.0 if unfilled).
        """
        intended_cost = intended_qty * intended_price
        actual_cost   = actual_qty   * actual_price
        cost_diff     = actual_cost  - intended_cost
        qty_diff      = actual_qty   - intended_qty

        # ── Fix _pos accounting ──────────────────────────────────────────────
        pos = self._pos[side]
        pos.qty  = max(0.0, pos.qty  + qty_diff)
        pos.cost = max(0.0, pos.cost + cost_diff)
        if pos.qty < 0.001:
            pos.qty = pos.cost = 0.0

        self.cash     -= cost_diff
        self.cash_out += cost_diff

        # ── Fix _rungs (decision state) ──────────────────────────────────────
        # Find last rung added for this side (the one _do_buy just appended).
        last_rung_idx = None
        for i in range(len(self._rungs) - 1, -1, -1):
            if self._rungs[i]['side'] == side:
                last_rung_idx = i
                break

        if actual_qty < 0.001:
            # Completely unfilled — remove ghost rung so logic is not misled
            if last_rung_idx is not None:
                removed = self._rungs.pop(last_rung_idx)
                print(f'[Ladder] reconcile_buy {side}: UNFILLED — removed ghost rung buy@{removed["buy_price"]:.3f}')
            self.trade_count = max(0, self.trade_count - 1)
            print(f'[Ladder] reconcile_buy {side}: UNFILLED — reversed paper buy')
        else:
            # Partial or full fill — sync rung to actual live fill
            if last_rung_idx is not None:
                r = self._rungs[last_rung_idx]
                r['qty']  = actual_qty
                r['cost'] = actual_cost
                # Update buy_price and recalculate TP/SL from LIVE price
                if actual_price > 0.01 and abs(actual_price - r['buy_price']) > 0.005:
                    old_bp = r['buy_price']
                    r['buy_price'] = actual_price
                    sl_dist = round(self.RUNG_SPREAD / self.MIN_RR, 4)
                    r['sell_target'] = round(actual_price + self.RUNG_SPREAD, 4)
                    r['stop_price'] = round(actual_price - sl_dist, 4)
                    print(f'[Ladder] reconcile_buy {side}: TP/SL updated: '
                          f'buy {old_bp:.3f}→{actual_price:.3f} '
                          f'TP={r["sell_target"]:.3f} SL={r["stop_price"]:.3f}')
            print(
                f'[Ladder] reconcile_buy {side}: intended={intended_qty:.2f}@{intended_price:.3f}'
                f' → actual={actual_qty:.2f}@{actual_price:.3f}'
                f' (qty_diff={qty_diff:+.2f} cost_diff=${cost_diff:+.2f})'
            )

    def reconcile_sell(self, side: str,
                       intended_qty: float, intended_price: float, intended_pnl: float,
                       actual_qty: float,   actual_price: float,
                       fail_reason: str = '', min_order_size: float = 5.0):
        """
        Correct paper position to match actual live SELL fill.
        intended_pnl = the pnl value from the _do_sell 5-tuple return.
        actual_qty   = 0.0 if the sell never executed.
        """
        # Cost-basis per share at time of sell (from realised pnl calculation)
        avg_cost_per_share = (intended_qty * intended_price - intended_pnl) / intended_qty \
            if intended_qty > 0.001 else intended_price
        paper_cost_basis = avg_cost_per_share * intended_qty

        # Undo paper sell
        pos = self._pos[side]
        pos.qty  += intended_qty
        pos.cost += paper_cost_basis
        self.cash         -= intended_qty * intended_price
        self.cash_in      -= intended_qty * intended_price
        self.realised_pnl -= intended_pnl

        # Apply actual sell
        if actual_qty > 0.001:
            actual_cost_basis = avg_cost_per_share * actual_qty
            actual_proceeds   = actual_qty * actual_price
            actual_pnl        = actual_proceeds - actual_cost_basis

            pos.qty  = max(0.0, pos.qty  - actual_qty)
            pos.cost = max(0.0, pos.cost - actual_cost_basis)
            if pos.qty < 0.001:
                pos.qty = pos.cost = 0.0
            self.cash         += actual_proceeds
            self.cash_in      += actual_proceeds
            self.realised_pnl += actual_pnl
            print(
                f'[Ladder] reconcile_sell {side}: intended={intended_qty:.2f}@{intended_price:.3f}'
                f' → actual={actual_qty:.2f}@{actual_price:.3f}'
            )
            # Partial fill — restore remainder as a live rung so the bot retries it
            remaining_qty = round(intended_qty - actual_qty, 4)
            if remaining_qty >= 0.5:
                for i in range(len(self._sold_rung_buffer) - 1, -1, -1):
                    if self._sold_rung_buffer[i]['side'] == side:
                        remainder = dict(self._sold_rung_buffer.pop(i))
                        remainder['qty']  = remaining_qty
                        remainder['cost'] = avg_cost_per_share * remaining_qty
                        remainder['hold_to_res'] = False
                        self._rungs.append(remainder)
                        _tag = ' (hold_to_res — below min size)' if remainder['hold_to_res'] else ''
                        print(f'[Ladder] reconcile_sell {side}: PARTIAL — {remaining_qty:.2f} remaining re-queued @ sell={remainder["sell_target"]:.3f} stop={remainder["stop_price"]:.3f}{_tag}')
                        break
        else:
            # Sell completely failed — position restored.
            # Also put the rung back so bot still tracks it for future sell.
            self.trade_count = max(0, self.trade_count - 1)

            # Don't abandon rungs — CLOB can sell any qty we own.
            # Transient failures (settlement, allowance) are retried next tick.
            hold_to_res = False

            # Find the most recent buffer entry for this side and restore it.
            for i in range(len(self._sold_rung_buffer) - 1, -1, -1):
                if self._sold_rung_buffer[i]['side'] == side:
                    restored = self._sold_rung_buffer.pop(i)
                    if hold_to_res:
                        restored['hold_to_res'] = True
                    self._rungs.append(restored)
                    tag = ' (hold_to_res)' if hold_to_res else ''
                    print(f'[Ladder] reconcile_sell {side}: UNFILLED — rung restored{tag} (sell@{restored["sell_target"]:.3f})')
                    break
            else:
                print(f'[Ladder] reconcile_sell {side}: UNFILLED — position restored (no rung in buffer)')

    # ════════════════════════════════════════════════════════════════════════
    # MAIN TRADING LOGIC
    # ════════════════════════════════════════════════════════════════════════
    def check_and_trade(
        self,
        up_price:       float,
        down_price:     float,
        timestamp:      str,
        time_to_close:  Optional[float] = None,
        up_bid:         Optional[float] = None,
        down_bid:       Optional[float] = None,
        up_orderbook:   Optional[dict]  = None,
        down_orderbook: Optional[dict]  = None,
    ) -> List[Tuple]:

        trades: List[Tuple] = []

        if self.market_status != 'open':
            self.current_mode = 'closed'
            return trades

        # ── clamp & store ──────────────────────────────────────────────────
        up = max(0.01, min(0.99, up_price))
        dn = max(0.01, min(0.99, down_price))

        self._ask['UP']   = up
        self._ask['DOWN'] = dn
        self._bid['UP']   = up_bid   if up_bid   and up_bid   > 0.01 else max(0.01, up - 0.005)
        self._bid['DOWN'] = down_bid if down_bid and down_bid > 0.01 else max(0.01, dn - 0.005)
        ttc   = time_to_close if time_to_close is not None else 99999.0

        self._tick += 1
        if self._tick == 1:
            self._market_start_ttc = ttc  # record TTC at open
            _ub = len((up_orderbook or {}).get('bids', [])) if up_orderbook else 0
            _ua = len((up_orderbook or {}).get('asks', [])) if up_orderbook else 0
            _db = len((down_orderbook or {}).get('bids', [])) if down_orderbook else 0
            _da = len((down_orderbook or {}).get('asks', [])) if down_orderbook else 0

        # ── Chaos detection: track imbalance flips + price direction changes ─
        # Imbalance flip tracking
        _cur_imb = up - 0.5  # simplified: positive = UP leading
        _cur_sign = 1 if _cur_imb > 0.005 else (-1 if _cur_imb < -0.005 else 0)
        if _cur_sign != 0 and self._imb_prev_sign != 0 and _cur_sign != self._imb_prev_sign:
            self._imb_flip_count += 1
        if _cur_sign != 0:
            self._imb_prev_sign = _cur_sign

        # Price direction tracking (use natural leading side)
        _natural_leading_price = max(up, dn)
        if self._prev_lead_price > 0:
            _price_dir = 1 if _natural_leading_price > self._prev_lead_price + 0.005 else (
                         -1 if _natural_leading_price < self._prev_lead_price - 0.005 else 0)
            if _price_dir != 0 and self._prev_price_dir != 0 and _price_dir != self._prev_price_dir:
                self._price_dir_flips += 1
            if _price_dir != 0:
                self._prev_price_dir = _price_dir
        self._prev_lead_price = _natural_leading_price

        # Rolling window reset
        self._chaos_window += 1
        if self._chaos_window >= self._CHAOS_WINDOW_SIZE:
            self._chaos_window = 0
            self._imb_flip_count = 0
            self._price_dir_flips = 0

        # Chaos mode disabled — was triggering premature sells in normal markets
        # if not self._chaos_mode and self._phase != 'scout':
        #     if (self._imb_flip_count >= self._CHAOS_FLIP_THRESHOLD
        #             or self._price_dir_flips >= self._CHAOS_PRICE_THRESHOLD):
        #         self._chaos_mode = True

        avail = self._available()

        # ── LOSS LIMIT: force-sell all positions and stop trading ──────────
        if self.MAX_LOSS_PER_MARKET > 0 and not self._loss_limit_hit:
            if self.realised_pnl <= -self.MAX_LOSS_PER_MARKET:
                self._loss_limit_hit = True
                print(f'[LadderMate] ⛔ LOSS LIMIT HIT: ${self.realised_pnl:+.2f} '
                      f'(limit -${self.MAX_LOSS_PER_MARKET:.2f}) — force-selling all positions')
        if self._loss_limit_hit:
            # Force-sell any remaining rungs at bid
            for rung in list(self._rungs):
                if rung.get('hold_to_res', False):
                    continue
                side = rung['side']
                bid_px = self._bid[side]
                if bid_px >= 0.05:
                    t = self._do_sell(side, bid_px, rung['qty'], 'LOSS_LIMIT_EXIT')
                    if t:
                        trades.append(t)
                        print(f'[LadderMate] ⛔ LOSS_LIMIT_EXIT {side} {rung["qty"]:.1f}'
                              f'@bid={bid_px:.3f} (bought@{rung["buy_price"]:.3f})')
                        self._rungs.remove(rung)
                        self._sold_rung_buffer.append(rung)
            self.current_mode = 'loss_limit'
            self.mode_reason = f'LOSS LIMIT: ${self.realised_pnl:+.2f}'
            return trades

        # ── CHAOS: sell everything, cooldown before re-entry ────────────────
        if self._chaos_mode:
            # Sell all open rungs
            for rung in list(self._rungs):
                if rung.get('hold_to_res', False):
                    continue
                side = rung['side']
                bid_px = self._bid[side]
                if bid_px >= 0.05:
                    t = self._do_sell(side, bid_px, rung['qty'], 'CHAOS_EXIT')
                    if t:
                        trades.append(t)
                        pnl_rung = rung['qty'] * (bid_px - rung['buy_price'])
                        print(f'[LadderMate] CHAOS_EXIT {side} {rung["qty"]:.1f}'
                              f'@bid={bid_px:.3f} (bought@{rung["buy_price"]:.3f})'
                              f' pnl=${pnl_rung:+.2f}')
                        self._rungs.remove(rung)
                        self._sold_rung_buffer.append(rung)
            # Stay in chaos mode until flip counters reset naturally
            # (window resets every _CHAOS_WINDOW_SIZE ticks)
            if (self._imb_flip_count < self._CHAOS_FLIP_THRESHOLD
                    and self._price_dir_flips < self._CHAOS_PRICE_THRESHOLD):
                # Market has calmed down — reset to scout
                self._chaos_mode = False
                self._phase = 'scout'
                self._ladder_side = None
                self._last_rung_price = 0.0
                print(f'[LadderMate] CHAOS CLEARED — resuming scout')
            self.current_mode = 'chaos'
            self.mode_reason = (f'CHAOS: imb_flips={self._imb_flip_count} '
                                f'price_flips={self._price_dir_flips} '
                                f'realised=${self.realised_pnl:+.2f}')
            return trades

        # ── effective params (always calm regime) ────────────────────────────
        sl_dist   = round(self.RUNG_SPREAD / self.MIN_RR, 4)
        max_rungs = self.MAX_RUNGS
        exit_ttc  = self.EXIT_TTC

        # ── ACTIVE EXIT WINDOW: sell open rungs at bid if liquid enough ──────
        if ttc < exit_ttc:
            exited = []
            now_exit = time.time()
            for rung in self._rungs:
                if rung.get('hold_to_res', False):
                    continue
                # Don't try to sell rungs that haven't settled on-chain yet
                age = now_exit - rung.get('buy_time', 0)
                if age < self.SETTLE_SECS:
                    continue
                side     = rung['side']
                bid_px   = self._bid[side]
                ask_px   = self._ask[side]
                # Only sell if there are real buyers at a reasonable price.
                # Below EXIT_MIN_BID the market has effectively decided against
                # this side — holding to resolution is better EV than selling.
                if bid_px < self.EXIT_MIN_BID:
                    continue
                # Use bid price for the exit sell (realistic fill)
                t = self._do_sell(side, bid_px, rung['qty'], 'EXIT_SELL')
                if t:
                    trades.append(t)
                    pnl_rung = rung['qty'] * (bid_px - rung['buy_price'])
                    print(f'[LadderMate] EXIT_SELL {side} {rung["qty"]:.1f}'
                          f'@bid={bid_px:.3f} (bought@{rung["buy_price"]:.3f}) '
                          f'ttc={ttc:.0f}s pnl=${pnl_rung:+.2f} '
                          f'realised=${self.realised_pnl:+.2f}')
                    exited.append(rung)
            for r in exited:
                self._rungs.remove(r)

        # ── HOLD: freeze all new activity in last 10 s ─────────────────────
        if ttc < self.HOLD_TTC:
            self.current_mode = 'hold'
            self.mode_reason  = (
                f'HOLD realised=${self.realised_pnl:+.2f} '
                f'open_rungs={len(self._rungs)} '
                f'U={self.qty_up:.1f}/${self.cost_up:.1f} '
                f'D={self.qty_down:.1f}/${self.cost_down:.1f}'
            )
            return trades

        price = {'UP': up, 'DOWN': dn}
        _natural_leading = 'UP' if up >= dn else 'DOWN'

        # ── Side selection (price-based) ────────────────────────────
        leading = _natural_leading

        if self.mirror_mode:
            leading = 'DOWN' if leading == 'UP' else 'UP'

        # ════════════════════════════════════════════════════════════════════
        # PHASE: SCOUT — find entry on the leading side
        # ════════════════════════════════════════════════════════════════════
        if self._phase == 'scout':
            # Original LadderMate: use price-based leading side for entry
            leading = _natural_leading
            lead_price = price[leading]
            # Use natural leading price for entry gate — it tells us whether
            # the market has clear direction, regardless of OBK or mirror pick.
            _gate_price = price[_natural_leading]

            elapsed = (self._market_start_ttc - ttc) if self._market_start_ttc >= 0 else 0.0
            if elapsed < self.WARMUP_SECS:
                self.current_mode = 'warmup'
                self.mode_reason  = (f'WARMUP {self.WARMUP_SECS - elapsed:.0f}s left'
                                     f' UP={up:.3f} DN={dn:.3f}')
                return trades

            if lead_price >= self.ENTRY_MIN_PRICE and lead_price <= self.ENTRY_MAX_PRICE:
                # Place first rung — orderbook confirms direction, price not too cheap
                _rung_usd = self.RUNG_USD + (self._market_rung_bonus if self.COMPOUND_RUNG_ENABLED else 0.0)
                t = self._do_buy(leading, lead_price, _rung_usd, 'RUNG_1')
                if t:
                    trades.append(t)
                    rung = {
                        'side': leading,
                        'buy_price':   lead_price,
                        'sell_target': round(lead_price + self.RUNG_SPREAD, 4),
                        'stop_price':  round(lead_price - sl_dist, 4),
                        'qty':  t[3],
                        'cost': t[3] * lead_price,
                        'hold_to_res': False,
                        'buy_time': time.time(),
                    }
                    self._rungs.append(rung)
                    self._ladder_side   = leading
                    self._last_rung_price = lead_price
                    self._phase = 'ladder'
                    print(f'[LadderMate] ENTRY {leading}@{lead_price:.3f} '
                          f'rung1 sell@{rung["sell_target"]:.3f} '
                          f'stop@{rung["stop_price"]:.3f}')

            self.current_mode = 'scout'
            self.mode_reason  = (
                f'SCOUT UP={up:.3f} DN={dn:.3f} '
                f'lead={leading}@{price[leading]:.3f} '
                f'gate={_gate_price:.3f}'
            )
            return trades

        # ════════════════════════════════════════════════════════════════════
        # SELL CHECK — orderbook-driven exits + never-give-up pending sells
        # ════════════════════════════════════════════════════════════════════
        now = time.time()

        # ── First: retry any pending sells that failed previously ──────────
        still_pending = []
        for ps in self._pending_sells:
            side = ps['side']
            bid_px = self._bid[side]
            if bid_px >= 0.05:
                t = self._do_sell(side, bid_px, ps['qty'], 'RETRY_SELL')
                if t:
                    trades.append(t)
                    pnl = ps['qty'] * (bid_px - ps['buy_price'])
                    print(f'[LadderMate] RETRY_SELL {side} {ps["qty"]:.1f}@{bid_px:.3f} '
                          f'(bought@{ps["buy_price"]:.3f}) pnl=${pnl:+.2f}')
                else:
                    still_pending.append(ps)
            else:
                still_pending.append(ps)
        self._pending_sells = still_pending

        # ── Price-based TP/SL sell signals ────────────────────────────────
        sell_batch: Dict[str, list] = {'UP': [], 'DOWN': []}
        for rung in self._rungs:
            if rung.get('hold_to_res', False):
                continue
            age = now - rung.get('buy_time', 0)
            side      = rung['side']
            cur_price = price[side]
            if cur_price >= rung['sell_target']:
                sell_batch[side].append(('RUNG_SELL', rung, cur_price))
            else:
                # Fixed stop-loss — no settlement window adjustment
                effective_sl = self.RUNG_SPREAD / self.MIN_RR
                effective_stop = round(rung['buy_price'] - effective_sl, 4)
                if cur_price <= effective_stop:
                    sell_batch[side].append(('RUNG_STOP', rung, cur_price))

        sold_rungs = []
        for side in ('UP', 'DOWN'):
            batch = sell_batch[side]
            if not batch:
                continue
            total_qty = sum(r['qty'] for _, r, _ in batch)
            sell_price = min(p for _, _, p in batch)
            t = self._do_sell(side, sell_price, total_qty, 'BATCH_SELL')
            if t:
                trades.append(t)
                sold_qty = t[3]
                remaining_budget = sold_qty
                for reason, rung, cp in batch:
                    if remaining_budget >= rung['qty'] - 0.001:
                        sold_rungs.append(rung)
                        remaining_budget -= rung['qty']
                        pnl_rung = rung['qty'] * (sell_price - rung['buy_price'])
                        print(f'[LadderMate] {reason} {side} {rung["qty"]:.1f}@{sell_price:.3f} '
                              f'(bought@{rung["buy_price"]:.3f}) '
                              f'rung_pnl=${pnl_rung:+.2f} '
                              f'realised=${self.realised_pnl:+.2f}')
                        # Compound rung sizing: roll TP profit into next rung
                        if reason == 'RUNG_SELL' and self.COMPOUND_RUNG_ENABLED and pnl_rung > 0:
                            old_bonus = self._market_rung_bonus
                            self._market_rung_bonus = min(
                                self._market_rung_bonus + pnl_rung,
                                self.COMPOUND_RUNG_MAX
                            )
                            print(f'[LadderMate] COMPOUND bonus ${old_bonus:.2f} -> ${self._market_rung_bonus:.2f} '
                                  f'(next rung=${self.RUNG_USD + self._market_rung_bonus:.2f})')
                    else:
                        print(f'[LadderMate] {side} rung {rung["qty"]:.1f}@{rung["buy_price"]:.3f} '
                              f'kept (batch partial: {remaining_budget:.2f} remaining)')
            else:
                # Sell failed — add to pending sells (never give up)
                for reason, rung, cp in batch:
                    self._pending_sells.append({
                        'side': side, 'qty': rung['qty'],
                        'buy_price': rung['buy_price'], 'reason': reason,
                    })
                    sold_rungs.append(rung)  # remove from _rungs, tracked in _pending_sells now
                    print(f'[LadderMate] SELL_QUEUED {reason} {side} {rung["qty"]:.1f} '
                          f'(bought@{rung["buy_price"]:.3f}) — will retry every tick')
            if self._pos[side].qty < 0.01 and not t:
                for _, rung, _ in batch:
                    if rung not in sold_rungs:
                        sold_rungs.append(rung)
                print(f'[LadderMate] ⚠️ {side} pos=0 but {len(batch)} rungs triggered — '
                      f'removing orphaned rungs (qty_sum={total_qty:.2f})')

        for r in sold_rungs:
            self._rungs.remove(r)
            self._sold_rung_buffer.append(r)
        if len(self._sold_rung_buffer) > 40:
            self._sold_rung_buffer = self._sold_rung_buffer[-40:]
        avail = self._available()

        # ── Recovery sell check ────────────────────────────────────────────
        if self._recovery_side and self._recovery_qty > 0.01:
            rec_price = price[self._recovery_side]
            if rec_price >= self._recovery_target:
                t = self._do_sell(
                    self._recovery_side, rec_price,
                    self._pos[self._recovery_side].qty,
                    'RECOVERY_SELL'
                )
                if t:
                    trades.append(t)
                    print(f'[LadderMate] RECOVERY SELL {self._recovery_side}'
                          f'@{rec_price:.3f} realised=${self.realised_pnl:+.2f}')
                    self._recovery_qty   = 0.0
                    self._recovery_side  = None
                    # After recovery sell, resume ladder on the new side
                    if self._ladder_side:
                        self._phase = 'ladder'
                avail = self._available()

        # ════════════════════════════════════════════════════════════════════
        # PHASE: LADDER — add new rungs as price climbs, watch for flip
        # ════════════════════════════════════════════════════════════════════
        if self._phase == 'ladder':

            ls          = self._ladder_side
            ls_price    = price[ls]
            other_side  = 'DOWN' if ls == 'UP' else 'UP'

            # ── flip detection (price + orderbook) ─────────────────────────
            price_flip = ls_price < self.FLIP_TRIGGER and leading != ls
            if price_flip:
                self._flip_ticks += 1
            else:
                self._flip_ticks = 0

            # Adaptive flip confirmation: require more ticks when market
            # is oscillating near 0.50 (no clear direction)
            _flip_threshold = self.FLIP_CONFIRM_TICKS
            if max(up, dn) < 0.58:
                _flip_threshold = 5  # cautious near 50/50
            if self._flip_ticks >= _flip_threshold:
                if ttc < exit_ttc:
                    # Inside exit window — ignore the flip signal entirely.
                    # Let existing rungs run to stop-loss, rung-sell or resolution.
                    # Firing a flip here costs bid-spread losses on rungs that may
                    # still resolve at $1.00 if the current side holds.
                    self._flip_ticks = 0
                    print(f'[LadderMate] FLIP suppressed (ttc={ttc:.0f}s < exit_ttc={exit_ttc}s)')
                else:
                    # ── EXECUTE FLIP (exit + restart) ─────────────────────
                    other_price = price[other_side]
                    open_cost   = self._open_rung_cost(ls)

                    # Sell all old-side rungs at current bid — accept the loss,
                    # free capital so the new-side ladder has full budget.
                    exited = []
                    now_flip = time.time()
                    for r in self._rungs:
                        if r['side'] == ls:
                            # Skip rungs not yet settled on-chain — leave in self._rungs
                            # so the regular sell check picks them up once settled.
                            age = now_flip - r.get('buy_time', 0)
                            if age < self.SETTLE_SECS:
                                print(f'[LadderMate] FLIP_PENDING {ls} {r["qty"]:.1f}@{r["buy_price"]:.3f} (unsettled {age:.0f}s) — will sell when settled')
                                continue  # NOT added to exited — stays in self._rungs
                            bid_px = self._bid[ls]
                            if bid_px >= 0.01:
                                t = self._do_sell(ls, bid_px, r['qty'], 'FLIP_EXIT')
                                if t:
                                    trades.append(t)
                                    exited.append(r)
                                    pnl_r = r['qty'] * (bid_px - r['buy_price'])
                                    print(f'[LadderMate] FLIP_EXIT {ls} {r["qty"]:.1f}'
                                          f'@bid={bid_px:.3f} '
                                          f'(bought@{r["buy_price"]:.3f}) '
                                          f'pnl=${pnl_r:+.2f}')
                                else:
                                    print(f'[LadderMate] ⚠️ FLIP_EXIT {ls} {r["qty"]:.1f}'
                                          f'@{r["buy_price"]:.3f} sell failed — rung kept')
                    for r in exited:
                        self._rungs.remove(r)
                    avail = self._available()

                    # Restart ladder fresh on the new side (no recovery position)
                    self._ladder_side     = other_side
                    self._last_rung_price = other_price
                    self._flip_ticks      = 0
                    self._phase           = 'ladder'
                    print(f'[LadderMate] FLIP {ls}->{other_side} (exit+restart) '
                          f'old_cost=${open_cost:.2f} pivot@{other_price:.3f} '
                          f'avail=${avail:.2f}')

            # ── add new rungs if price has climbed ─────────────────────────
            if self._phase == 'ladder':  # may have just become flip_recover
                open_count = self._open_rung_count(ls)
                # Guard: don't add rungs if existing rungs have unsold
                # profit or stop targets. Prevents uncontrolled exposure
                # when live sells fail (e.g. empty book).
                # Block new rungs if ANY open rung exists on ANY side.
                # Must sell ALL existing rungs before buying new ones.
                _has_open_rung = any(
                    not r.get('hold_to_res', False)
                    for r in self._rungs
                )
                _has_unsold = _has_open_rung
                # Orderbook rung gate: skip if book turned against us
                if (not _has_unsold
                        
                        and ls_price >= (0.40 if self.mirror_mode else 0.20)
                        and ttc >= exit_ttc                       # ← never open new positions in exit window
                        and ls_price >= self._last_rung_price + self.RUNG_STEP
                        and ls_price <= self.MAX_RUNG_PRICE
                        and open_count < max_rungs
                        and avail >= self.RUNG_USD
                        and self._pos[ls].cost + self.RUNG_USD <= self.MAX_SIDE_COST):
                    _rung_usd = self.RUNG_USD + (self._market_rung_bonus if self.COMPOUND_RUNG_ENABLED else 0.0)
                    t = self._do_buy(ls, ls_price, _rung_usd, f'RUNG_{open_count+1}')
                    if t:
                        trades.append(t)
                        target   = round(ls_price + self.RUNG_SPREAD, 4)
                        stop_p   = round(ls_price - sl_dist, 4)
                        self._rungs.append({
                            'side':        ls,
                            'buy_price':   ls_price,
                            'sell_target': target,
                            'stop_price':  stop_p,
                            'qty':  t[3],
                            'cost': t[3] * ls_price,
                            'hold_to_res': False,
                            'buy_time': time.time(),
                        })
                        self._last_rung_price = ls_price
                        print(f'[LadderMate] RUNG {ls}@{ls_price:.3f} '
                              f'sell@{target:.3f} stop@{stop_p:.3f} '
                              f'open={self._open_rung_count(ls)}')
                    avail = self._available()

        # ════════════════════════════════════════════════════════════════════
        # PHASE: flip_recover — legacy phase; should not be entered in
        # exit+restart mode, but handled gracefully just in case.
        # ════════════════════════════════════════════════════════════════════
        elif self._phase == 'flip_recover':
            # Upgrade to ladder immediately on next tick
            self._phase = 'ladder'
            ls         = self._ladder_side
            ls_price   = price[ls] if ls else 0.0
            other_side = 'DOWN' if ls == 'UP' else 'UP'
            open_count = self._open_rung_count(ls) if ls else 0
            # Phase will be upgraded to 'ladder' on the next call; nothing else to do.

        # ── update display ─────────────────────────────────────────────────
        open_rungs  = len(self._rungs)
        best        = self.best_case_profit
        rec_info    = ''
        if self._recovery_side:
            rec_info = (f' rec={self._recovery_side}@{self._recovery_target:.3f}'
                        f' qty={self._recovery_qty:.1f}')
        ls_disp = self._ladder_side or '---'
        self.current_mode = self._phase
        self.mode_reason  = (
            f'{self._phase.upper()} ladder={ls_disp}'
            f'@{price.get(ls_disp, 0):.3f}'
            f' rungs={open_rungs} realised=${self.realised_pnl:+.2f}'
            f' best=${best:+.2f}'
            f' flip_budget=${self._flip_budget:.2f}'
            f' U={self.qty_up:.1f}/${self.cost_up:.1f}'
            f' D={self.qty_down:.1f}/${self.cost_down:.1f}'
            + rec_info
        )
        return trades

    # ════════════════════════════════════════════════════════════════════════
    # Market resolution
    # ════════════════════════════════════════════════════════════════════════
    def resolve_market(self, outcome: str) -> float:
        self.market_status      = 'resolved'
        self.resolution_outcome = outcome

        up = self._pos['UP']
        dn = self._pos['DOWN']

        # Unsold shares pay out $1 if outcome matches, $0 otherwise
        payout         = up.qty if outcome == 'UP' else dn.qty
        remaining_cost = up.cost + dn.cost
        resolution_pnl = payout - remaining_cost

        self.cash         += payout
        self.cash_in      += payout
        self.payout        = payout
        self.realised_pnl += resolution_pnl

        self.trade_log.append({
            'side':   outcome, 'reason': 'RESOLVED',
            'signal': f'RES_{outcome}',
            'entry':  0, 'exit': 1.0 if payout > 0 else 0.0,
            'qty':    up.qty + dn.qty,
            'pnl':    resolution_pnl, 'mode': 'resolution',
        })

        self.final_pnl       = self.realised_pnl
        self.final_pnl_gross = self.realised_pnl
        self.last_fees_paid  = 0.0

        up.clear()
        dn.clear()
        self._rungs.clear()
        self._market_rung_bonus = 0.0  # reset compound bonus for new market
        return self.final_pnl

    # ════════════════════════════════════════════════════════════════════════
    # Stubs (web_bot_multi.py compatibility)
    # ════════════════════════════════════════════════════════════════════════
    def update_spot_price(self, price: float, timestamp=None): pass
    def set_market_open_spot(self, price: float): pass

    def reset_predictor_for_new_market(self):
        self._spot_prediction = None
        self._spot_confidence = 0.0
        self._spot_reason     = ''

    def get_state(self) -> dict:
        up     = self._pos['UP']
        dn     = self._pos['DOWN']
        pnl_up = self._pnl_if('UP')
        pnl_dn = self._pnl_if('DOWN')
        net    = self.cash_out - self.cash_in

        # Summarise open rungs for display
        open_rungs_up   = self._open_rung_count('UP')
        open_rungs_down = self._open_rung_count('DOWN')

        return {
            'qty_up':            up.qty,
            'qty_down':          dn.qty,
            'cost_up':           up.cost,
            'cost_down':         dn.cost,
            'avg_up':            self.avg_up,
            'avg_down':          self.avg_down,
            'pair_cost':         self.pair_cost,
            'locked_profit':     self.realised_pnl,
            'best_case_profit':  self.best_case_profit,
            'qty_ratio':         self.qty_ratio,
            'balance_pct':       0.0,
            'is_balanced':       True,
            'trade_count':       self.trade_count,
            'market_status':     self.market_status,
            'resolution_outcome': self.resolution_outcome,
            'final_pnl':         self.final_pnl,
            'final_pnl_gross':   self.final_pnl_gross,
            'fees_paid':         0.0,
            'payout':            self.payout,
            'max_hedge_up':      0.0,
            'max_hedge_down':    0.0,
            'current_mode':      self.current_mode,
            'mode_reason':       self.mode_reason,
            'cash_out':          self.cash_out,
            'cash_in':           self.cash_in,
            'arb_locked':        self._pair_locked,
            'main_side':         self._ladder_side or '---',
            'flip_counter':      self._flip_ticks,
            'flip_threshold':    self.FLIP_CONFIRM_TICKS,
            'realised_pnl':      self.realised_pnl,
            'net_invested':      net,
            'pnl_if_up_wins':    pnl_up,
            'pnl_if_down_wins':  pnl_dn,
            'up_entry':          self.avg_up,
            'down_entry':        self.avg_down,
            'up_stop':           0.0,
            'down_stop':         0.0,
            'up_signal':         up.signal,
            'down_signal':       dn.signal,
            'obk_score_up':      0.0,
            'obk_score_down':    0.0,
            'profit_goal':       self.PROFIT_GOAL,
            'goal_reached':      self.realised_pnl >= self.PROFIT_GOAL,
            'loss_limit':        self.LOSS_LIMIT,
            'loss_limit_hit':    self.realised_pnl <= self.LOSS_LIMIT,
            # ladder-specific extras (shown in debug logs)
            'open_rungs_up':     open_rungs_up,
            'open_rungs_down':   open_rungs_down,
            'recovery_target':   self._recovery_target,
            'ladder_side':       self._ladder_side or '---',
        }
