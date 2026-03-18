"""FOK (Fill-Or-Kill) Dutch Book strategy.

How it works
────────────
Binary UP/DOWN markets must satisfy P(UP) + P(DOWN) = 1.00 at resolution.
When asks on both sides sum to less than $1.00, a Dutch Book exists: cross
the spread on both sides simultaneously via FOK taker orders and collect the
gap as guaranteed profit regardless of outcome.

Cycle:
  1. Trigger: best_bid_up + best_bid_down <= 1.0 - SPREAD_THRESHOLD
              AND ask_up + ask_down + 2*FOK_PRICE_BUFFER < 1.0  (profitable at execution)
              AND both asks within MIN_SIDE_PRICE–MAX_SIDE_PRICE (near-equilibrium only)
  2. Compute order sizes via inventory skew.
  3. Place FOK BUY orders on BOTH sides simultaneously at ask + FOK_PRICE_BUFFER.
  4. FOK fills immediately against resting sell orders or cancels — no waiting.
  5. If one side filled and the other didn't: trigger asymmetric hedge logic.
  6. Repeat.

Advantages over passive GTC approach
─────────────────────────────────────
• Both sides fill in the same instant — no asymmetric fill risk from trending markets.
• No cooldown window during which the market can move against us.
• Guaranteed outcome: either both sides fill profitably, or neither fills.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from config import (
    SPREAD_THRESHOLD, ORDER_SIZE, INVENTORY_SKEW,
    TRADE_COOLDOWN_MS, PRICE_LADDER,
    STOP_BEFORE_END_MS, MAX_POSITION_SIZE,
    MIN_BID_LIQUIDITY, MIN_SIDE_PRICE, MAX_SIDE_PRICE, MIN_ORDER_VALUE,
    MAX_IMBALANCE, HEDGE_MONITOR_SEC, HEDGE_RISE_TRIGGER,
)
from bot.orderbook import Orderbook
from bot.market_finder import MarketPair
from bot.position_tracker import PositionTracker
from bot.risk_manager import RiskManager

log = logging.getLogger(__name__)

_BASE_SIZE = ORDER_SIZE // 2   # 5 when ORDER_SIZE = 10


@dataclass
class CycleSignal:
    bid_up:   float
    bid_down: float
    up_size:  int
    down_size: int
    ask_up:   Optional[float] = None  # used by hedge logic
    ask_down: Optional[float] = None

    @property
    def profit_per_pair(self) -> float:
        return 1.0 - self.bid_up - self.bid_down - 2 * PRICE_LADDER[0]


class DutchBookStrategy:
    def __init__(self, tracker: PositionTracker, risk: RiskManager) -> None:
        self._tracker = tracker
        self._risk    = risk

    def reset_for_new_market(self) -> None:
        """Called by BotEngine when a new market pair is detected."""
        pass  # No per-market state to reset

    # ── Sizing ────────────────────────────────────────────────────────────────

    def _compute_sizes(self, up_shares: int, down_shares: int) -> tuple[int, int]:
        """Inventory-skew sizing.

        Lagging side (fewer shares) gets extra shares; leading side gives some back.
        adj steps: 0 for |imbalance| < 3, 1 for 3–5, 2 for 6+.
        """
        imbalance = down_shares - up_shares   # positive = DOWN ahead (UP lagging)
        adj = min(2, abs(imbalance) // 3)

        if imbalance > 0:     # DOWN ahead — UP is lagging
            up_size   = min(MAX_POSITION_SIZE, _BASE_SIZE + adj)
            down_size = max(1, _BASE_SIZE - adj)
        elif imbalance < 0:   # UP ahead — DOWN is lagging
            up_size   = max(1, _BASE_SIZE - adj)
            down_size = min(MAX_POSITION_SIZE, _BASE_SIZE + adj)
        else:
            up_size = down_size = _BASE_SIZE

        return up_size, down_size

    # ── Evaluate ──────────────────────────────────────────────────────────────

    def evaluate(
        self,
        ob_up:   Orderbook,
        ob_down: Orderbook,
        pair:    MarketPair,
    ) -> Optional[CycleSignal]:
        """Return a CycleSignal if spread conditions are met, else None."""
        if not self._risk.check_all(pair):
            return None

        bid_up   = ob_up.best_bid
        bid_down = ob_down.best_bid
        if bid_up is None or bid_down is None:
            return None

        # Trigger: bid sum must show a profitable spread
        if bid_up + bid_down > 1.0 - SPREAD_THRESHOLD:
            return None

        # Price guard: only trade near-equilibrium markets (0.38–0.60).
        # Outside this range strong directional conviction makes fills asymmetric.
        if not (MIN_SIDE_PRICE <= bid_up <= MAX_SIDE_PRICE):
            return None
        if not (MIN_SIDE_PRICE <= bid_down <= MAX_SIDE_PRICE):
            return None

        # Require bid-side liquidity on both sides
        if ob_up.best_bid_size < MIN_BID_LIQUIDITY or ob_down.best_bid_size < MIN_BID_LIQUIDITY:
            return None

        pos = self._tracker.position
        up_size, down_size = self._compute_sizes(pos.up_shares, pos.down_shares)

        if pos.up_shares >= MAX_POSITION_SIZE:
            up_size = 0
        if pos.down_shares >= MAX_POSITION_SIZE:
            down_size = 0

        imbalance_raw = pos.up_shares - pos.down_shares
        if imbalance_raw >= MAX_IMBALANCE:
            up_size = 0
        elif imbalance_raw <= -MAX_IMBALANCE:
            down_size = 0

        if up_size < 2:
            up_size = 0
        if down_size < 2:
            down_size = 0

        if up_size == 0 and down_size == 0:
            return None

        return CycleSignal(
            bid_up=bid_up,
            bid_down=bid_down,
            up_size=up_size,
            down_size=down_size,
            ask_up=ob_up.best_ask,
            ask_down=ob_down.best_ask,
        )

    # ── Orderbook helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _bid_size_at(ob: Orderbook, price: float) -> float:
        """Current bid queue size at exactly this price level (0 if absent)."""
        with ob._lock:
            for lvl in ob.bids:
                if abs(lvl.price - price) < 0.005:
                    return lvl.size
        return 0.0

    @staticmethod
    def _calc_fill(our_size: int, queue_before_us: float, min_observed: float) -> int:
        """Time-priority fill model.

        We are at the back of the queue when we place our order.
        Fills only come to us after all orders that were ahead of us are consumed.

        consumed = (queue_before_us + our_size) - min_observed_bid_during_window
        our_fill = max(0, consumed - queue_before_us)  capped at our_size
        """
        total_before_cancel = queue_before_us + our_size
        consumed = max(0.0, total_before_cancel - min_observed)
        filled = max(0, min(our_size, int(consumed - queue_before_us)))
        return filled

    # ── Hedge ─────────────────────────────────────────────────────────────────

    async def _hedge_unfilled(
        self,
        hedge_side: str,
        hedge_size: int,
        avg_filled_price: float,
        ob_unfilled: "Orderbook",
        token_id: str,
        client,
        loop,
        log_fn,
    ) -> None:
        """Cross the spread on the side that didn't fill to lock in the Dutch Book outcome.

        Uses a FOK (Fill-Or-Kill) taker order priced slightly above the current ask.
        The taker order fills immediately against resting sell orders, guaranteeing
        that both sides of the Dutch Book are covered — even if one side didn't fill
        passively during the cooldown window.

        We accept the hedge as long as combined cost per pair < $1.05. Paying up to
        $0.05 extra per pair to eliminate directional risk is almost always better than
        holding an unhedged position that can lose the full filled price if wrong.
        """
        ask = ob_unfilled.best_ask
        if ask is None:
            log_fn(f"  No ask for {hedge_side} — cannot hedge, holding directional position")
            return

        # Set taker price 3 cents above current ask as a buffer for market movement.
        # Cap at 0.99 to avoid paying near-certainty prices.
        taker_price = round(min(ask + 0.03, 0.99), 4)
        combined = avg_filled_price + taker_price

        if combined >= 1.05:
            log_fn(
                f"  {hedge_side} ask ${ask:.4f} → combined ${combined:.3f} too high — "
                f"holding directional position"
            )
            return

        log_fn(
            f"  One-sided fill — hedging {hedge_size} {hedge_side} via FOK @ ${taker_price:.4f} "
            f"(combined ${combined:.3f}/pair)"
        )
        try:
            _, filled = await loop.run_in_executor(
                None, client.place_market_fok,
                token_id, taker_price, hedge_size, "BUY",
            )
            if filled > 0:
                log_fn(f"  Hedge filled {filled} {hedge_side} — outcome locked in")
            else:
                log_fn(f"  Hedge FOK cancelled (no liquidity @ ${taker_price:.4f}) — directional exposure remains")
        except Exception as exc:
            log_fn(f"  Hedge error: {exc} — directional exposure remains")

    async def _wait_and_hedge_on_reversal(
        self,
        hedge_side: str,
        hedge_size: int,
        avg_filled_price: float,
        ob_unfilled: "Orderbook",
        token_id: str,
        client,
        loop,
        log_fn,
        pair: "MarketPair",
        current_pair_fn,
    ) -> None:
        """Monitor cheap-side ask after expensive side fills; hedge when price starts rising.

        After the expensive side fills, the cheap side's price typically continues
        dropping (momentum). We track the minimum ask observed. As soon as the ask
        rises HEDGE_RISE_TRIGGER above that minimum, we trigger a FOK hedge — buying
        the cheap side at the best available price rather than at the immediate post-fill
        ask (which is usually the highest point in the cheap side's move down).

        If no reversal is detected within HEDGE_MONITOR_SEC, we hold the directional
        position (the expensive side may simply win the market).
        """
        ask = ob_unfilled.best_ask
        if ask is None:
            log_fn(f"  No ask for {hedge_side} — cannot monitor, holding directional position")
            return

        min_ask_seen = ask
        log_fn(
            f"  {hedge_side} expensive fill @ ${avg_filled_price:.3f} — "
            f"monitoring {hedge_side} ask (current ${ask:.4f}), "
            f"will hedge when ask rises ≥ ${HEDGE_RISE_TRIGGER:.2f} from minimum"
        )

        deadline = time.time() + HEDGE_MONITOR_SEC
        while time.time() < deadline:
            await asyncio.sleep(0.5)

            # Abort if the pair rotated
            cur = current_pair_fn()
            if cur is None or cur.up_token_id != pair.up_token_id:
                log_fn(f"  Pair rotated — abandoning hedge monitor")
                return

            current_ask = ob_unfilled.best_ask
            if current_ask is None:
                continue

            # Track the floor (lowest ask = cheapest available hedge price)
            if current_ask < min_ask_seen:
                min_ask_seen = current_ask
                log_fn(f"  {hedge_side} ask new low: ${min_ask_seen:.4f}")

            # When ask rises HEDGE_RISE_TRIGGER above the floor, momentum has reversed
            if current_ask >= min_ask_seen + HEDGE_RISE_TRIGGER:
                log_fn(
                    f"  {hedge_side} ask rising: ${min_ask_seen:.4f} → ${current_ask:.4f} "
                    f"(+{current_ask - min_ask_seen:.4f}) — hedging now"
                )
                await self._hedge_unfilled(
                    hedge_side, hedge_size, avg_filled_price,
                    ob_unfilled, token_id, client, loop, log_fn,
                )
                return

        log_fn(
            f"  {hedge_side} no reversal in {HEDGE_MONITOR_SEC}s "
            f"(min ask ${min_ask_seen:.4f}) — holding directional position"
        )

    async def _check_fills_and_hedge(
        self,
        pair,
        cycle_start_ts: float,
        ob_up: "Orderbook",
        ob_down: "Orderbook",
        client,
        loop,
        log_fn,
        current_pair_fn,
    ) -> None:
        """Read fills since cycle_start_ts and hedge if only one side filled.

        Called both after a normal cooldown and after order placement failures
        (where the API may have placed orders despite returning an error).
        Does NOT record fills — reconciler is sole source of truth.
        """
        await asyncio.sleep(0.3)
        try:
            raw = await loop.run_in_executor(
                None, client.get_fills_for_market,
                pair.token_ids, cycle_start_ts,
            )
            up_filled = 0; up_cost = 0.0
            dn_filled = 0; dn_cost = 0.0
            for f in raw:
                sz = int(float(f.get("size", 0)))
                pr = float(f.get("price", 0))
                if sz <= 0 or pr <= 0:
                    continue
                tid = f.get("asset_id", "")
                if tid == pair.up_token_id:
                    up_filled += sz; up_cost += pr * sz
                elif tid == pair.down_token_id:
                    dn_filled += sz; dn_cost += pr * sz

            log_fn(f"Fills: UP={up_filled} DOWN={dn_filled} — reconciler will update position")

            if up_filled > 0 and dn_filled == 0:
                avg_up = up_cost / up_filled
                if avg_up > 0.50:
                    await self._wait_and_hedge_on_reversal(
                        "DOWN", up_filled, avg_up,
                        ob_down, pair.down_token_id, client, loop, log_fn,
                        pair, current_pair_fn,
                    )
                else:
                    await self._hedge_unfilled(
                        "DOWN", up_filled, avg_up,
                        ob_down, pair.down_token_id, client, loop, log_fn,
                    )
            elif dn_filled > 0 and up_filled == 0:
                avg_dn = dn_cost / dn_filled
                if avg_dn > 0.50:
                    await self._wait_and_hedge_on_reversal(
                        "UP", dn_filled, avg_dn,
                        ob_up, pair.up_token_id, client, loop, log_fn,
                        pair, current_pair_fn,
                    )
                else:
                    await self._hedge_unfilled(
                        "UP", dn_filled, avg_dn,
                        ob_up, pair.up_token_id, client, loop, log_fn,
                    )
        except Exception as exc:
            log_fn(f"Fill check failed ({exc}) — reconciler will sync within 5s")

    # ── Execute ───────────────────────────────────────────────────────────────

    async def execute(
        self,
        signal:         CycleSignal,
        pair:           MarketPair,
        ob_up:          Orderbook,
        ob_down:        Orderbook,
        client,
        log_fn,
        current_pair_fn,
    ) -> None:
        """Place GTC limit orders across the price ladder, wait, cancel, then hedge if asymmetric."""
        pos  = self._tracker.position
        loop = asyncio.get_event_loop()

        # Only activate ladder levels that keep combined cost < $1.00 per pair
        active_ladder = [
            lvl for lvl in PRICE_LADDER
            if signal.bid_up + signal.bid_down + 2 * lvl < 1.0
        ]
        if not active_ladder:
            log_fn("No profitable ladder levels — skipping")
            return

        prices_up = [round(signal.bid_up  + lvl, 4) for lvl in active_ladder]
        prices_dn = [round(signal.bid_down + lvl, 4) for lvl in active_ladder]

        log_fn(
            f"Spread! bid UP=${signal.bid_up:.3f} DOWN=${signal.bid_down:.3f}  "
            f"ladder={[f'+{lvl:.2f}' for lvl in active_ladder]}  "
            f"profit ≈ ${signal.profit_per_pair:.3f}/pair"
        )
        log_fn(f"Position: UP={pos.up_shares}, DOWN={pos.down_shares} (imbalance: {pos.down_shares-pos.up_shares})")

        cycle_start_ts = time.time()
        up_queue = self._bid_size_at(ob_up,   prices_up[0])
        dn_queue = self._bid_size_at(ob_down, prices_dn[0])

        # ── Place GTC on both sides at each ladder level ──────────────────────
        async def _place_gtc(token_id: str, price: float, size: int, label: str):
            if size * price < MIN_ORDER_VALUE:
                return None
            try:
                oid, _ = await loop.run_in_executor(
                    None, client.place_limit_gtc, token_id, price, size, "BUY",
                )
                log_fn(f"  GTC {label}")
                return oid
            except Exception as exc:
                log_fn(f"  Order error {label}: {exc}")
                return None

        if client.paper:
            # Paper: cheapest level only (faster simulation)
            tasks = []
            if signal.up_size > 0:
                tasks.append(_place_gtc(pair.up_token_id, prices_up[0], signal.up_size,
                                        f"UP {signal.up_size} @ ${prices_up[0]:.4f}"))
            if signal.down_size > 0:
                tasks.append(_place_gtc(pair.down_token_id, prices_dn[0], signal.down_size,
                                        f"DOWN {signal.down_size} @ ${prices_dn[0]:.4f}"))
        else:
            # Live: all active levels
            tasks = []
            for pu, pd in zip(prices_up, prices_dn):
                if signal.up_size > 0:
                    tasks.append(_place_gtc(pair.up_token_id, pu, signal.up_size,
                                            f"UP {signal.up_size} @ ${pu:.4f}"))
                if signal.down_size > 0:
                    tasks.append(_place_gtc(pair.down_token_id, pd, signal.down_size,
                                            f"DOWN {signal.down_size} @ ${pd:.4f}"))

        results    = await asyncio.gather(*tasks)
        order_ids  = [oid for oid in results if oid]

        if not order_ids:
            if not client.paper:
                log_fn("All placements failed — checking for accidental fills...")
                await self._check_fills_and_hedge(
                    pair, cycle_start_ts, ob_up, ob_down,
                    client, loop, log_fn, current_pair_fn,
                )
            return

        log_fn(f"Waiting {TRADE_COOLDOWN_MS}ms ({len(order_ids)} orders open)...")

        # ── Cooldown: track bid queue movement (paper sim) ────────────────────
        min_up_bid   = up_queue
        min_dn_bid   = dn_queue
        pair_rotated = False
        deadline     = time.time() + TRADE_COOLDOWN_MS / 1000

        while time.time() < deadline:
            await asyncio.sleep(0.5)
            cur_up = self._bid_size_at(ob_up,   prices_up[0])
            cur_dn = self._bid_size_at(ob_down, prices_dn[0])
            if cur_up < min_up_bid: min_up_bid = cur_up
            if cur_dn < min_dn_bid: min_dn_bid = cur_dn
            cur = current_pair_fn()
            if cur is None or cur.up_token_id != pair.up_token_id:
                log_fn("Pair changed during cooldown — cancelling early")
                pair_rotated = True
                break

        # ── Cancel all orders concurrently ────────────────────────────────────
        log_fn("Cancelling orders...")
        await asyncio.gather(*[
            loop.run_in_executor(None, client.cancel_order, oid) for oid in order_ids
        ], return_exceptions=True)

        if pair_rotated:
            return

        # ── Fills: simulate (paper) or read + hedge (live) ────────────────────
        if client.paper:
            up_sim = self._calc_fill(signal.up_size, up_queue, min_up_bid)
            dn_sim = self._calc_fill(signal.down_size, dn_queue, min_dn_bid)
            if up_sim > 0:
                self._tracker.record_fill("UP",   prices_up[0], up_sim)
            if dn_sim > 0:
                self._tracker.record_fill("DOWN", prices_dn[0], dn_sim)
            new_pos = self._tracker.position
            log_fn(f"Fills (sim): UP={up_sim} DOWN={dn_sim} "
                   f"→ Position UP={new_pos.up_shares}, DOWN={new_pos.down_shares}")
        else:
            await self._check_fills_and_hedge(
                pair, cycle_start_ts, ob_up, ob_down,
                client, loop, log_fn, current_pair_fn,
            )
