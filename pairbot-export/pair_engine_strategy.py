"""
pair_engine_strategy.py  —  PairEngine v13 – Conviction Entry + Dynamic Hedge
══════════════════════════════════════════════════════════════════════════════

PHILOSOPHY
  Make a calculated directional bet at market open on the expensive
  (leading) side. Build the position until net theoretical profit
  reaches $10 if that side wins. Then hedge with cheap shares on the
  other side in case the market flips. Always grab arb when available.

PHASES (state machine, one per market):

  SCOUT   First COOLDOWN_SECONDS (10 s): observe only.

  BUILD   Buy the leading side dynamically each tick.
          Target: net_if_lead_wins = qty_lead - total_spent >= $10.
          First buy = ENTRY_USD ($5).  Subsequent = dynamic $1-$5.

  HEDGE   $10 target reached on lead side.  Now buy small amounts
          of the lagging side each tick as flip insurance.
          Stop when cost_lag >= total_spent * HEDGE_RATIO (25 %).

  SWITCH  If lead price falls below SWITCH_BELOW (0.55) the market
          has flipped. Swap build_side to the other side, repeat
          BUILD then HEDGE on the new leader (one switch allowed).

  ARB     Always: if combined < ARB_THRESHOLD (0.95), buy equal
          qty both sides to lock guaranteed profit.

RISK
  - MAX_SIDE_COST ($80) hard cap per side per market.
  - Total budget $500.  Losses bounded by hedge coverage.
  - Profit target $10 per win; target/loss ratio >= 2.5×.
══════════════════════════════════════════════════════════════════════════════
"""

import time
from collections import deque
from typing import List, Tuple, Optional, Dict, Any


class _DummyPredictor:
    current_spot_price: Optional[float] = None
    market_open_price: Optional[float] = None
    def update_spot_price(self, *a, **kw): pass
    def set_market_open_price(self, *a, **kw): pass
    def reset_for_new_market(self, *a, **kw): pass
    def predict(self): return None, 0.0, ''
    def record_market_outcome(self, *a, **kw): pass


class _Position:
    __slots__ = ('qty', 'cost', 'entry', 'signal', 'entry_time')

    def __init__(self):
        self.qty = 0.0
        self.cost = 0.0
        self.entry = 0.0
        self.signal = ''
        self.entry_time = 0.0

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty > 0 else 0.0

    def add(self, qty: float, price: float):
        self.cost += qty * price
        self.qty += qty
        self.entry = self.avg

    def remove(self, qty: float) -> float:
        qty = min(qty, self.qty)
        if self.qty <= 0:
            return 0.0
        avg = self.avg
        cost_removed = avg * qty
        self.qty -= qty
        self.cost -= cost_removed
        if self.qty < 0.01:
            self.qty = 0.0
            self.cost = 0.0
        return cost_removed

    def clear(self):
        self.qty = 0.0
        self.cost = 0.0
        self.entry = 0.0
        self.signal = ''


class PairEngineStrategy:
    """PairEngine v13 — Conviction Entry + Dynamic Hedge"""

    STRATEGY_NAME = 'PairEngine_v13_ConvictionHedge'

    # ── budget ────────────────────────────────────────────────────────
    MARKET_BUDGET    = 250.0
    MAX_PER_SIDE     = 125.0
    MAX_SIDE_COST    = 125.0  # hard spend cap per side per market

    # ── timing ────────────────────────────────────────────────────────
    COOLDOWN_SECONDS  = 60    # observe market for 60 s before entering
    PHASE_HOLD        = 15    # freeze last 15 s

    # ── chop filter (measured during cooldown, before any trades) ────
    CHOP_FLIP_PRICE   = 0.520 # a "flip" = a side crossing above this price
    CHOP_MAX_FLIPS    = 7     # skip market if > this many flips in cooldown window

    # ── entry & build (leading side) ─────────────────────────────────
    ENTRY_USD        = 5.00   # first buy on leading side at market open
    BUILD_FACTOR     = 0.40   # dynamic size = gap_to_target * BUILD_FACTOR
    BUILD_MAX_USD    = 5.00   # max single-tick build
    BUILD_MAX_PRICE  = 0.82   # don't start building if lead price > this at entry time
    MIN_ENTRY_SPREAD = 0.15   # minimum |UP-DOWN| spread to enter directionally
    # (below 0.15 the market is too undecided — hedge can't offset enough)
    PROFIT_TARGET    = 10.00  # net profit target if build-side wins ($)

    # ── switch condition ──────────────────────────────────────────────
    SWITCH_BELOW          = 0.62   # if build-side price drops below this AND other leads, switch
    SWITCH_LEAD_TICKS     = 2      # switch if other side has led for this many consecutive ticks
    SWITCH_COOLDOWN_TICKS = 4      # min ticks between consecutive switches (prevents flip-flop)
    BUILD_STOP_TTC        = 90     # stop building new positions when ttc < this many seconds

    # ── hedge (lagging side) ──────────────────────────────────────────
    HEDGE_RATIO      = 0.40   # buy lag until cost_lag = 40% of total_spent (covers ~40% of loss)
    HEDGE_USD        = 2.00   # per-tick hedge buy
    HEDGE_MAX_PRICE  = 0.60   # only hedge when lag is cheap (< 60c)

    # ── arb (always-on, highest priority) ─────────────────────────────
    ARB_THRESHOLD    = 0.95   # combined < this = locked guaranteed profit
    ARB_USD_PER_SIDE = 3.00   # $ per side per arb tick

    # ── misc ──────────────────────────────────────────────────────────
    MIN_TRADE        = 1.00   # Polymarket minimum
    MIN_PRICE        = 0.04
    MAX_PRICE        = 0.96
    PROFIT_GOAL      = 10.00
    MAX_LOSS         = 5.00   # worst-case loss cap: freeze new buys if min(pnl_UP, pnl_DN) <= -MAX_LOSS
    LOSS_LIMIT       = -5.00  # display alias (used in get_state / dashboard)

    # ── legacy aliases (web_bot_multi.py compat) ─────────────────────
    TREND_BUY_SIZE       = 1.00
    ARB_BUY_SIZE         = 3.00
    FLIP_BUY_SIZE        = 1.00
    TREND_COOLDOWN_TICKS = 1
    ARB_COOLDOWN_TICKS   = 1
    FLIP_COOLDOWN_TICKS  = 1
    FLIP_CONFIRM_TICKS   = 2
    MAX_PAIR_COST        = 0.95
    PAIR_COST_CAP        = 0.95
    FLIP_RECOVERY_MAX_PRICE = 0.90
    MIN_BALANCE_RATIO    = 0.77
    MAX_BALANCE_PRICE    = 0.88
    REBALANCE_CHEAP      = 0.88
    REBALANCE_BUDGET     = 250.0
    MAX_ENTRY_PRICE      = 0.95
    MIN_SPREAD           = 0.06
    OBK_MIN_DEPTH        = 20.0
    MEAN_WINDOW          = 12
    HISTORY_LEN          = 60
    TICK_PAIR_USD        = 3.00
    TICK_TILT_USD        = 1.00
    REBAL_USD            = 1.00
    TILT_RATIO_MAX       = 1.30
    MIN_SPREAD_TILT      = 0.08
    MAX_TILT_PRICE       = 0.88
    PROFIT_CAP           = 999.0

    # ══════════════════════════════════════════════════════════════════
    def __init__(self, market_budget=None, starting_balance=1000.0, exec_sim=None):
        self.market_budget     = market_budget or self.MARKET_BUDGET
        self.starting_balance  = starting_balance
        self.cash_ref          = {'balance': starting_balance}

        self._pos: Dict[str, _Position] = {
            'UP': _Position(), 'DOWN': _Position()
        }

        self._up_hist: deque = deque(maxlen=self.HISTORY_LEN)
        self._dn_hist: deque = deque(maxlen=self.HISTORY_LEN)
        self._tick: int = 0

        # dynamic primary (updated on flips)
        self._primary: Optional[str] = None
        self._flip_counter: int       = 0   # consecutive ticks other side leads

        # ── v13 phase state machine ───────────────────────────────────
        # Phases: 'scout' → 'build' → 'hedge' (→ 'switch_build' → 'switch_hedge')
        self._phase: str              = 'scout'
        self._build_side: Optional[str] = None   # which side we're building toward $10
        self._entered: bool           = False     # True after first buy placed
        self._switched: bool          = False     # True once we've done one switch
        self._arb_fired: int          = 0         # arb buy count this market
        self._hedge_target_abs: float = 0.0       # fixed $ target for hedge spend (set at hedge entry)
        self._lag_lead_ticks: int     = 0          # consecutive ticks where other side leads (secondary switch)
        self._last_switch_tick: int   = -99        # tick index of most recent switch (cooldown)
        self._loss_cap_logged: bool   = False       # log loss-cap trigger only once per market

        # ── chop filter state (populated during cooldown) ─────────────
        self._chop_flips: int         = 0          # leadership flips seen during cooldown
        self._chop_leader: str        = ''         # current leader tracked for flip counting
        self._chop_skip: bool         = False      # True = market too choppy, skip all entries

        # per-side cooldowns
        self._last_buy_tick: Dict[str, int] = {'UP': -99, 'DOWN': -99}

        # trend / mode tracking
        self._trend_score: float       = 0.0
        self._trend_side: Optional[str] = None

        # accounting
        self.cash_out      = 0.0
        self.cash_in       = 0.0
        self.realised_pnl  = 0.0
        self.trade_count   = 0
        self.trade_log: List[dict] = []

        self.market_status      = 'open'
        self.current_mode       = 'scout'
        self.mode_reason        = ''
        self.resolution_outcome: Optional[str] = None
        self.final_pnl:          Optional[float] = None
        self.final_pnl_gross:    Optional[float] = None
        self.last_fees_paid:     float = 0.0
        self.payout:             float = 0.0

        self._ask: Dict[str, float] = {'UP': 0.50, 'DOWN': 0.50}
        self._bid: Dict[str, float] = {'UP': 0.495, 'DOWN': 0.495}
        self._obk_score: Dict[str, float] = {'UP': 0.0, 'DOWN': 0.0}

        self._pair_locked = False

        # compat stubs
        self._arb_locked  = False
        self._main_side: Optional[str] = None
        self.trend_predictor   = _DummyPredictor()
        self._spot_prediction: Optional[str] = None
        self._spot_confidence: float = 0.0
        self._spot_reason: str = ''

    # ══════════════════════════════════════════════════════════════════
    # Properties
    # ══════════════════════════════════════════════════════════════════
    @property
    def cash(self) -> float:
        return self.cash_ref['balance']
    @cash.setter
    def cash(self, v: float):
        self.cash_ref['balance'] = v

    @property
    def qty_up(self)  -> float: return self._pos['UP'].qty
    @property
    def qty_down(self) -> float: return self._pos['DOWN'].qty
    @property
    def cost_up(self)  -> float: return self._pos['UP'].cost
    @property
    def cost_down(self) -> float: return self._pos['DOWN'].cost
    @property
    def avg_up(self)   -> float: return self._pos['UP'].avg
    @property
    def avg_down(self) -> float: return self._pos['DOWN'].avg

    @property
    def pair_cost(self) -> float:
        if self.qty_up > 0.01 and self.qty_down > 0.01:
            return self.avg_up + self.avg_down
        return 0.0

    @property
    def locked_profit(self) -> float:
        return self._guaranteed_pnl()

    @property
    def best_case_profit(self) -> float:
        return max(self._pnl_if('UP'), self._pnl_if('DOWN'))

    @property
    def qty_ratio(self) -> float:
        u, d = self.qty_up, self.qty_down
        if u < 0.01 or d < 0.01:
            return 0.0
        return max(u, d) / min(u, d)

    @property
    def total_spent(self) -> float:
        return self._pos['UP'].cost + self._pos['DOWN'].cost

    # ══════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════
    def calculate_locked_profit(self) -> float:
        return self._guaranteed_pnl()

    def calculate_total_fees(self) -> float:
        return 0.0

    def _available(self) -> float:
        spent   = self._pos['UP'].cost + self._pos['DOWN'].cost
        budget_r = max(0.0, self.market_budget - spent)
        return min(max(0.0, self.cash), budget_r)

    def _guaranteed_pnl(self) -> float:
        return min(self._pnl_if('UP'), self._pnl_if('DOWN'))

    def _pnl_if(self, outcome: str) -> float:
        up = self._pos['UP']
        dn = self._pos['DOWN']
        payout = up.qty if outcome == 'UP' else dn.qty
        cost   = up.cost + dn.cost
        return payout - cost + self.realised_pnl

    def _pair_cost_after(self, side: str, price: float, qty: float = 1.0) -> float:
        """Projected avg_up + avg_down if we buy qty of side at price.
        Returns 0.0 when only one side would be held (no pair constraint)."""
        up = self._pos['UP']
        dn = self._pos['DOWN']
        if side == 'UP':
            new_up_avg = (up.cost + price * qty) / (up.qty + qty)
            new_dn_avg = dn.avg
            have_both  = dn.qty > 0.01
        else:
            new_up_avg = up.avg
            new_dn_avg = (dn.cost + price * qty) / (dn.qty + qty)
            have_both  = up.qty > 0.01
        return (new_up_avg + new_dn_avg) if have_both else 0.0

    def _rolling_mean(self, side: str) -> Optional[float]:
        hist = self._up_hist if side == 'UP' else self._dn_hist
        if len(hist) < self.MEAN_WINDOW:
            return None
        vals = list(hist)[-self.MEAN_WINDOW:]
        return sum(vals) / len(vals)

    def _book_imbalance(self, orderbook: Optional[dict]) -> float:
        if not orderbook:
            return 0.0
        def _vol(levels):
            v = 0.0
            for lvl in levels[:5]:
                if isinstance(lvl, dict):
                    v += float(lvl.get('size', 0) or lvl.get('quantity', 0))
                elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    v += float(lvl[1])
            return v
        bids  = orderbook.get('bids', [])
        asks  = orderbook.get('asks', [])
        bid_v = _vol(bids)
        ask_v = _vol(asks)
        total = bid_v + ask_v
        if total < self.OBK_MIN_DEPTH:
            return 0.0
        return (bid_v - ask_v) / total

    def _buy(self, side: str, price: float, usd: float, reason: str = '') \
            -> Optional[Tuple]:
        usd = min(usd, self._available())
        if usd < self.MIN_TRADE:
            return None
        if not (self.MIN_PRICE <= price <= self.MAX_PRICE):
            return None
        qty = usd / price
        self.cash    -= usd
        self.cash_out += usd
        self.trade_count += 1
        p = self._pos[side]
        p.add(qty, price)
        p.signal     = reason
        p.entry_time = time.time()
        self._last_buy_tick[side] = self._tick
        return ('BUY', side, price, qty)

    # ── trend score (used for mode display only) ──────────────────────
    def _compute_trend_score(self) -> float:
        scores, weights = [], []
        if len(self._up_hist) >= 8:
            up_vals = list(self._up_hist)[-8:]
            up_mean = sum(up_vals) / len(up_vals)
            up_now  = self._up_hist[-1]
            m = max(-1.0, min(1.0, (up_now - up_mean) / max(0.01, up_mean) * 10.0))
            scores.append(m); weights.append(0.40)
        if len(self._up_hist) >= 5:
            recent   = list(self._up_hist)[-5:]
            velocity = (recent[-1] - recent[0]) / 5
            v = max(-1.0, min(1.0, velocity / 0.01))
            scores.append(v); weights.append(0.35)
        up_obk  = self._obk_score.get('UP', 0.0)
        dn_obk  = self._obk_score.get('DOWN', 0.0)
        o = max(-1.0, min(1.0, (up_obk - dn_obk) * 2.0))
        scores.append(o); weights.append(0.25)
        if not scores:
            return 0.0
        total_w  = sum(weights)
        weighted = sum(s * w for s, w in zip(scores, weights)) / total_w
        return max(-1.0, min(1.0, weighted))

    # ══════════════════════════════════════════════════════════════════
    # MAIN TRADING LOGIC  (v13)
    # ══════════════════════════════════════════════════════════════════
    def check_and_trade(
        self,
        up_price: float,
        down_price: float,
        timestamp: str,
        time_to_close: Optional[float] = None,
        up_bid: Optional[float] = None,
        down_bid: Optional[float] = None,
        up_orderbook: Optional[dict] = None,
        down_orderbook: Optional[dict] = None,
    ) -> List[Tuple[str, str, float, float]]:

        trades: List[Tuple[str, str, float, float]] = []

        if self.market_status != 'open':
            self.current_mode = 'closed'
            return trades

        # ── record prices ──────────────────────────────────────────
        up = max(0.01, min(0.99, up_price))
        dn = max(0.01, min(0.99, down_price))

        self._ask['UP']   = up
        self._ask['DOWN'] = dn
        self._bid['UP']   = up_bid   if (up_bid   and up_bid   > 0.01) else max(0.01, up - 0.005)
        self._bid['DOWN'] = down_bid if (down_bid and down_bid > 0.01) else max(0.01, dn - 0.005)

        self._up_hist.append(up)
        self._dn_hist.append(dn)
        self._tick += 1

        self._obk_score['UP']   = self._book_imbalance(up_orderbook)
        self._obk_score['DOWN'] = self._book_imbalance(down_orderbook)

        ttc     = time_to_close if time_to_close is not None else 99999.0
        elapsed = 300.0 - ttc

        # ── COOLDOWN + CHOP FILTER ────────────────────────────────
        if elapsed < self.COOLDOWN_SECONDS:
            # track leadership flips (each time a side crosses above CHOP_FLIP_PRICE)
            cur_leader = 'UP' if up >= dn else 'DOWN'
            if self._chop_leader == '':
                self._chop_leader = cur_leader
            elif cur_leader != self._chop_leader:
                # only count if winning side is actually decisively above threshold
                if (up if cur_leader == 'UP' else dn) > self.CHOP_FLIP_PRICE:
                    self._chop_flips += 1
                    self._chop_leader = cur_leader
            self.current_mode = 'scout'
            self.mode_reason  = (f'cooldown {elapsed:.0f}/{self.COOLDOWN_SECONDS}s '
                                 f'flips={self._chop_flips}/{self.CHOP_MAX_FLIPS}')
            return trades

        # ── evaluate chop filter exactly once at cooldown expiry ──
        if not self._entered and not self._chop_skip and self._chop_flips == 0:
            # first tick after cooldown: finalise chop decision
            pass  # chop_flips already populated above
        if not self._entered and self._chop_flips > self.CHOP_MAX_FLIPS and not self._chop_skip:
            self._chop_skip = True
            print(f'[v13] CHOP_SKIP flips={self._chop_flips} > {self.CHOP_MAX_FLIPS} '
                  f'— skipping market (too volatile at open)')

        # ── SKIP choppy markets (ARB still allowed) ────────────────
        if self._chop_skip:

            # still run ARB in choppy markets — fall through to ARB block below
            # but skip all directional trading
            self.current_mode = 'chop_skip'
            self.mode_reason  = f'CHOP flips={self._chop_flips} — ARB only'
            # allow execution to continue only into ARB block, then return
            self._trend_score = self._compute_trend_score()
            self._trend_side  = 'UP' if self._trend_score > 0 else ('DOWN' if self._trend_score < 0 else None)
            combined = up + dn
            avail    = self._available()
            if combined < self.ARB_THRESHOLD and avail >= self.MIN_TRADE * 2:
                up_cost = round(self.ARB_USD_PER_SIDE / up, 4) * up
                dn_cost = round(self.ARB_USD_PER_SIDE / dn, 4) * dn
                t_up = self._buy('UP',   up, up_cost, 'ARB')
                t_dn = self._buy('DOWN', dn, dn_cost, 'ARB')
                if t_up: trades.append(t_up)
                if t_dn: trades.append(t_dn)
            return trades

        # ── HOLD ───────────────────────────────────────────────────
        if ttc < self.PHASE_HOLD:
            gp = self._guaranteed_pnl()
            bc = self.best_case_profit
            self.current_mode = 'hold'
            self.mode_reason  = f'HOLD guar=${gp:+.2f} best=${bc:+.2f}'
            if not self._pair_locked and self.total_spent > 0:
                self._pair_locked = True
                print(f'[v13] LOCKED phase={self._phase} '
                      f'build={self._build_side} guar=${gp:+.2f} best=${bc:+.2f} '
                      f'U={self.qty_up:.1f}/${self.cost_up:.1f} '
                      f'D={self.qty_down:.1f}/${self.cost_down:.1f}')
            return trades

        self._trend_score = self._compute_trend_score()
        self._trend_side  = 'UP' if self._trend_score > 0 else ('DOWN' if self._trend_score < 0 else None)

        combined = up + dn
        spread   = abs(up - dn)
        avail    = self._available()
        pos_up   = self._pos['UP']
        pos_dn   = self._pos['DOWN']

        # current market leader
        leading  = 'UP' if up >= dn else 'DOWN'
        lagging  = 'DOWN' if leading == 'UP' else 'UP'
        lead_ask = up if leading == 'UP' else dn
        lag_ask  = dn if leading == 'UP' else up

        # ── legacy _primary tracking (compat) ─────────────────────
        if self._primary is None:
            self._primary = leading
        elif leading != self._primary:
            self._flip_counter += 1
            if self._flip_counter >= self.FLIP_CONFIRM_TICKS:
                self._primary = leading
                self._flip_counter = 0
        else:
            self._flip_counter = max(0, self._flip_counter - 1)

        # ══════════════════════════════════════════════════════════════
        # ARB  —  always-on, highest priority
        # If combined < 0.95, buying equal qty locks guaranteed profit.
        # ══════════════════════════════════════════════════════════════
        if combined < self.ARB_THRESHOLD and avail >= self.MIN_TRADE * 2:
            min_p    = min(up, dn)
            qty_arb  = self.ARB_USD_PER_SIDE / min_p
            up_cost  = qty_arb * up
            dn_cost  = qty_arb * dn
            if up_cost >= self.MIN_TRADE and dn_cost >= self.MIN_TRADE \
                    and up_cost + dn_cost <= avail:
                t_up = self._buy('UP',   up, up_cost, 'ARB')
                t_dn = self._buy('DOWN', dn, dn_cost, 'ARB')
                if t_up: trades.append(t_up)
                if t_dn: trades.append(t_dn)
                avail = self._available()
                self._arb_fired += 1
                if self._tick % 5 == 0:
                    gp = self._guaranteed_pnl()
                    print(f'[v13] ARB UP@{up:.3f}+DN@{dn:.3f}={combined:.3f} '
                          f'guar=${gp:+.2f} (fire #{self._arb_fired})')

        # ══════════════════════════════════════════════════════════════
        # LOSS CAP — freeze directional buying if worst-case >= -MAX_LOSS
        # ARB already ran above and is unaffected (it locks guaranteed profit).
        # Existing positions are untouched — they resolve at $1.00 if correct.
        # ══════════════════════════════════════════════════════════════
        worst_case = min(self._pnl_if('UP'), self._pnl_if('DOWN'))
        lag_side = 'DOWN' if (self._build_side == 'UP') else 'UP'
        hedge_started = self._pos[lag_side].cost >= 1.0 if self._build_side else False
        if hedge_started and worst_case < -self.MAX_LOSS:
            if not self._loss_cap_logged:
                self._loss_cap_logged = True
                print(f'[v13] LOSS_CAP worst=${worst_case:+.2f} <= -${self.MAX_LOSS:.2f} '
                      f'— freezing new buys, holding to resolution '
                      f'U={self.qty_up:.1f}/${self.cost_up:.1f} '
                      f'D={self.qty_down:.1f}/${self.cost_down:.1f}')
            self.current_mode = 'loss_cap'
            self.mode_reason  = (f'LOSS CAP worst=${worst_case:+.2f} '
                                 f'build={self._build_side} '
                                 f'U=${self.cost_up:.1f} D=${self.cost_down:.1f}')
            return trades

        # ══════════════════════════════════════════════════════════════
        # SWITCH CHECK  (unlimited switches — always follow the trend)
        # Trigger A: build-side price < SWITCH_BELOW AND other side leads
        # Trigger B: other side has led for SWITCH_LEAD_TICKS consecutive ticks
        # Cooldown:  no switch within SWITCH_COOLDOWN_TICKS ticks of last switch
        # ══════════════════════════════════════════════════════════════
        ticks_since_switch = self._tick - self._last_switch_tick
        if self._build_side is not None \
                and self._phase in ('build', 'hedge') \
                and ticks_since_switch >= self.SWITCH_COOLDOWN_TICKS:
            other_side_is_leading = (leading != self._build_side)
            if other_side_is_leading:
                self._lag_lead_ticks += 1
            else:
                self._lag_lead_ticks = 0

            build_price_now = up if self._build_side == 'UP' else dn
            trigger_a = build_price_now < self.SWITCH_BELOW and other_side_is_leading
            trigger_b = self._lag_lead_ticks >= self.SWITCH_LEAD_TICKS
            if trigger_a or trigger_b:
                old      = self._build_side
                self._build_side     = 'DOWN' if old == 'UP' else 'UP'
                self._switched       = True
                self._phase          = 'build'   # restart build on new side
                self._lag_lead_ticks = 0          # reset counter after switch
                self._last_switch_tick = self._tick
                print(f'[v13] SWITCH {old}->{self._build_side} '
                      f'(price={build_price_now:.3f} lag_ticks={self._lag_lead_ticks} '
                      f'trigger={"A" if trigger_a else "B"})')

        # ── resolve convenience vars for current build target ─────
        if self._build_side is None:
            self._build_side = leading     # first active tick: pick leader

        bs       = self._build_side
        ls       = 'DOWN' if bs == 'UP' else 'UP'   # lag / hedge side
        bp       = up  if bs == 'UP' else dn         # build-side current ask
        lp       = dn  if bs == 'UP' else up         # lag-side current ask
        pos_b    = self._pos[bs]
        pos_l    = self._pos[ls]

        # net profit if build side pays out $1 at resolution
        net_build = pos_b.qty - self.total_spent

        # ══════════════════════════════════════════════════════════════
        # PHASE: scout  →  fire first entry buy
        # Skip entry if lead price is already too high (> BUILD_MAX_PRICE):
        # at 0.85+ the risk/reward is too poor to safely reach $10 net.
        # In that case, stay in scout and let ARB-only handle the market.
        # ══════════════════════════════════════════════════════════════
        if self._phase == 'scout':
            if bp > self.BUILD_MAX_PRICE:
                self.current_mode = 'scout'
                self.mode_reason  = (
                    f'SKIP entry {bs}@{bp:.3f} > MAX_BUILD_PRICE {self.BUILD_MAX_PRICE} '
                    f'(arb-only mode)'
                )
            elif spread < self.MIN_ENTRY_SPREAD:
                self.current_mode = 'scout'
                self.mode_reason  = (
                    f'SKIP entry spread={spread:.3f} < MIN_ENTRY_SPREAD {self.MIN_ENTRY_SPREAD} '
                    f'(market too undecided)'
                )
            elif avail >= self.MIN_TRADE:
                entry_usd = min(self.ENTRY_USD, avail)
                t = self._buy(bs, bp, entry_usd, 'ENTRY')
                if t:
                    trades.append(t)
                    avail = self._available()
                    self._entered = True
                    self._phase   = 'build'
                    print(f'[v13] ENTRY {bs}@{bp:.3f} ${entry_usd:.2f} '
                          f'net_target=${self.PROFIT_TARGET:.0f}')

        # ══════════════════════════════════════════════════════════════
        # PHASE: build  →  keep buying build-side until net >= $10
        # ══════════════════════════════════════════════════════════════
        elif self._phase == 'build':
            if net_build >= self.PROFIT_TARGET:
                self._phase = 'hedge'
                # Lock hedge target now: HEDGE_RATIO × build cost at this moment.
                # Using a fixed value avoids a runaway loop where hedge spend
                # grows total_spent, which grows the hedge target indefinitely.
                self._hedge_target_abs = self._pos[bs].cost * self.HEDGE_RATIO
                print(f'[v13] TARGET net_build=${net_build:+.2f} -> HEDGE '
                      f'hedge_target=${self._hedge_target_abs:.1f}')
            elif avail >= self.MIN_TRADE and pos_b.cost < self.MAX_SIDE_COST \
                    and ttc > self.BUILD_STOP_TTC:
                gap = max(0.0, self.PROFIT_TARGET - net_build)
                usd = max(self.MIN_TRADE, min(self.BUILD_MAX_USD, gap * self.BUILD_FACTOR))
                t = self._buy(bs, bp, usd, 'BUILD')
                if t:
                    trades.append(t)
                    avail = self._available()
                    if self._tick % 8 == 0:
                        print(f'[v13] BUILD {bs}@{bp:.3f} ${usd:.2f} '
                              f'net=${net_build:+.2f}/{self.PROFIT_TARGET:.0f} '
                              f'cost=${pos_b.cost:.1f}')

        # ══════════════════════════════════════════════════════════════
        # PHASE: hedge / switch_hedge
        # Buy cheap lag-side as flip insurance up to HEDGE_RATIO of total.
        # Also top-up build side if net drifted below target.
        # ══════════════════════════════════════════════════════════════
        elif self._phase in ('hedge', 'switch_hedge'):
            # small top-up if net drifted far below target
            if (net_build < self.PROFIT_TARGET - 3.0
                    and avail >= self.MIN_TRADE
                    and pos_b.cost < self.MAX_SIDE_COST):
                gap    = max(0.0, self.PROFIT_TARGET - net_build)
                top_up = max(self.MIN_TRADE, min(2.0, gap * 0.4))
                t = self._buy(bs, bp, top_up, 'TOPUP')
                if t:
                    trades.append(t)
                    avail = self._available()

            # hedge the lag side — stop at the FIXED target set at hedge entry
            hedge_target = self._hedge_target_abs
            if (pos_l.cost < hedge_target
                    and lp <= self.HEDGE_MAX_PRICE
                    and lp >= self.MIN_PRICE
                    and avail >= self.MIN_TRADE
                    and pos_l.cost < self.MAX_SIDE_COST):
                hedge_usd = max(self.MIN_TRADE,
                                min(self.HEDGE_USD, hedge_target - pos_l.cost))
                t = self._buy(ls, lp, hedge_usd, 'HEDGE')
                if t:
                    trades.append(t)
                    avail = self._available()
                    if self._tick % 5 == 0:
                        flip_if_win = pos_l.qty - self.total_spent
                        print(f'[v13] HEDGE {ls}@{lp:.3f} ${hedge_usd:.2f} '
                              f'flip_net=${flip_if_win:+.2f} '
                              f'hedge={pos_l.cost:.1f}/{hedge_target:.1f}')

        # After switch: check if new build target is also met
        if self._switched and self._phase == 'build' and net_build >= self.PROFIT_TARGET:
            self._phase = 'switch_hedge'
            self._hedge_target_abs = self._pos[bs].cost * self.HEDGE_RATIO
            print(f'[v13] SWITCH TARGET net=${net_build:+.2f} -> SWITCH_HEDGE '
                  f'hedge_target=${self._hedge_target_abs:.1f}')

        # ── update display mode ────────────────────────────────────
        gp  = self._guaranteed_pnl()
        bc  = self.best_case_profit
        self.current_mode = self._phase
        self.mode_reason  = (
            f'c={combined:.3f} sprd={spread:.3f} '
            f'build={bs}@{bp:.3f} lag={ls}@{lp:.3f} '
            f'net_build=${net_build:+.2f} guar=${gp:+.2f} best=${bc:+.2f} '
            f'U={self.qty_up:.1f}/${self.cost_up:.1f} '
            f'D={self.qty_down:.1f}/${self.cost_down:.1f}'
        )
        return trades

    # ══════════════════════════════════════════════════════════════════
    # Market resolution
    # ══════════════════════════════════════════════════════════════════
    def resolve_market(self, outcome: str) -> float:
        self.market_status      = 'resolved'
        self.resolution_outcome = outcome

        up = self._pos['UP']
        dn = self._pos['DOWN']

        payout         = up.qty if outcome == 'UP' else dn.qty
        remaining_cost = up.cost + dn.cost
        resolution_pnl = payout - remaining_cost

        self.cash     += payout
        self.cash_in  += payout
        self.payout    = payout
        self.realised_pnl += resolution_pnl

        self.trade_log.append({
            'side': outcome, 'reason': 'RESOLVED',
            'signal': f'RES_{outcome}',
            'entry': 0, 'exit': 1.0 if payout > 0 else 0.0,
            'qty': up.qty + dn.qty,
            'pnl': resolution_pnl, 'mode': 'resolution',
        })

        self.final_pnl       = self.realised_pnl
        self.final_pnl_gross = self.realised_pnl
        self.last_fees_paid  = 0.0

        up.clear()
        dn.clear()
        return self.final_pnl

    # ══════════════════════════════════════════════════════════════════
    # Stubs (web_bot_multi compatibility)
    # ══════════════════════════════════════════════════════════════════
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

        return {
            'qty_up': up.qty, 'qty_down': dn.qty,
            'cost_up': up.cost, 'cost_down': dn.cost,
            'avg_up': self.avg_up, 'avg_down': self.avg_down,
            'pair_cost': self.pair_cost,
            'locked_profit': self._guaranteed_pnl(),
            'best_case_profit': max(pnl_up, pnl_dn),
            'qty_ratio': self.qty_ratio,
            'balance_pct': 0.0,
            'is_balanced': self.qty_ratio < 1.5 if self.qty_ratio > 0 else True,
            'trade_count': self.trade_count,
            'market_status': self.market_status,
            'resolution_outcome': self.resolution_outcome,
            'final_pnl': self.final_pnl,
            'final_pnl_gross': self.final_pnl_gross,
            'fees_paid': 0.0,
            'payout': self.payout,
            'max_hedge_up': 0.0,
            'max_hedge_down': 0.0,
            'current_mode': self.current_mode,
            'mode_reason': self.mode_reason,
            'cash_out': self.cash_out,
            'cash_in': self.cash_in,
            'arb_locked': self._pair_locked,
            'main_side': self._primary or self._trend_side or '---',
            'flip_counter': self._flip_counter,
            'flip_threshold': self.FLIP_CONFIRM_TICKS,
            'realised_pnl': self.realised_pnl,
            'net_invested': net,
            'pnl_if_up_wins': pnl_up,
            'pnl_if_down_wins': pnl_dn,
            'up_entry': self.avg_up,
            'down_entry': self.avg_down,
            'up_stop': 0.0,
            'down_stop': 0.0,
            'up_signal': up.signal,
            'down_signal': dn.signal,
            'obk_score_up': self._obk_score.get('UP', 0.0),
            'obk_score_down': self._obk_score.get('DOWN', 0.0),
            'profit_goal': self.PROFIT_GOAL,
            'goal_reached': self.realised_pnl >= self.PROFIT_GOAL,
            'loss_limit': self.LOSS_LIMIT,
            'loss_limit_hit': self.realised_pnl <= self.LOSS_LIMIT,
        }
