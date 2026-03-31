#!/usr/bin/env python3
"""
live_executor.py -- Live order execution bridge for Polymarket CLOB

Reads credentials from .env -- NEVER hardcode keys in source code.

LIVE_TRADING=false (default)  -> delegates to ExecutionSimulator (paper only)
LIVE_TRADING=true             -> real orders placed via Polymarket CLOB API

All blocking network I/O is async-safe:
  - HTTP calls run in a thread-pool executor (never block the event loop)
  - Order polling uses asyncio.sleep (non-blocking)
  - A continuous background task polls CLOB every 0.5s for settlement
    confirmation after each BUY, so simulate_sell never blocks.
"""

import asyncio
import os
import copy
import time
import logging
from typing import Optional
from dotenv import load_dotenv

from execution_simulator import ExecutionSimulator, FillResult

load_dotenv()

logger = logging.getLogger(__name__)

# -- Safety caps ---------------------------------------------------------------
MAX_SINGLE_ORDER_USD  = 8.0    # hard cap per single order
MAX_OPEN_EXPOSURE_USD = 20.0   # total $ across all live open positions
MAX_BUYS_PER_MARKET   = 9      # max BUY orders per side per 15-min market window
BUY_COOLDOWN_S        = 15     # seconds between BUY attempts on same side (was 60)


class LiveExecutor:
    """
    Wraps ExecutionSimulator and, when LIVE_TRADING=true, also sends
    real market orders to Polymarket CLOB.

    Exposes the same interface as ExecutionSimulator so web_bot_multi.py
    needs zero changes to its existing executor hooks:
        simulate_buy(side, price, qty, orderbook)
        simulate_sell(side, price, qty, orderbook)
        set_token_ids(up_id, down_id)
        _get_token_id(side)
        get_token_balance(token_id)
    """

    def __init__(self, latency_ms: float = 25.0, max_slippage_pct: float = 2.0):
        # Paper simulator always runs (for stats / UI)
        self._sim = ExecutionSimulator(
            latency_ms=latency_ms,
            max_slippage_pct=max_slippage_pct,
        )

        self.live = os.getenv('LIVE_TRADING', 'false').strip().lower() == 'true'

        self._api_key        = os.getenv('POLY_API_KEY', '')
        self._api_secret     = os.getenv('POLY_API_SECRET', '')
        self._api_passphrase = os.getenv('POLY_API_PASSPHRASE', '')
        self._wallet_address = os.getenv('POLY_WALLET_ADDRESS', '')
        self._private_key    = os.getenv('POLY_PRIVATE_KEY', '')

        self._up_token_id:   Optional[str] = None
        self._down_token_id: Optional[str] = None
        self._client         = None
        self._open_exposure  = 0.0   # running total $ in live open positions
        self._approved_token_ids: set = set()  # token_ids with confirmed CLOB allowance
        self._buy_count: dict = {}   # {token_id: count} — buys in current market window
        self._last_buy_time: dict = {}  # {side: timestamp} — cooldown per side
        self._token_position_qty: dict = {}   # {token_id: open_qty} live filled qty tracking
        self._token_position_cost: dict = {}  # {token_id: open_cost} live cost-basis tracking
        self._sell_balance_retries: int = 0   # fast-retry counter for "not enough balance" on SELL
        self._buy_in_flight: set = set()      # sides with a live BUY currently in progress
        self._pending_tp_order: dict = {}    # {token_id: {'order_id': str, 'price': float, 'qty': float, 'side': str}}
        self._deferred_gtc_sl: dict = {}
        self._settlement_confirmed_real: dict = {}  # only True when shares were actually visible    # {token_id: {'side': str, 'price': float, 'qty': float}} — post after settlement

        # Settlement tracking — replaces all time.sleep() blocking
        # {token_id: bool} — True once CLOB confirms the token balance is visible
        self._settlement_ready: dict = {}
        self._settlement_created: dict = {}  # token_id → time.time() when first marked pending
        self._poller_task: Optional[asyncio.Task] = None
        self._order_executor = None   # dedicated ThreadPoolExecutor for sign + POST
        self._keepalive_task: Optional[asyncio.Task] = None  # CLOB TCP keepalive
        # orderbook cache removed...

        if self.live:
            self._init_client()
        else:
            print('[LiveExecutor] PAPER MODE -- no real orders will be sent.')

    # -- Delegate unknown attrs to inner simulator (stats, logs, etc.) ---------
    def __getattr__(self, name: str):
        # Only called when normal attribute lookup fails
        return getattr(self._sim, name)

    # -- Token ID management ---------------------------------------------------
    def set_token_ids(self, up_token_id: str, down_token_id: str):
        # If tokens changed (new market), reset buy counters
        if up_token_id != self._up_token_id or down_token_id != self._down_token_id:
            self._buy_count = {}
            self._last_buy_time = {}
            self._pending_tp_order = {}
            self._deferred_gtc_sl = {}
            self._settlement_confirmed_real = {}
        self._up_token_id   = up_token_id
        self._down_token_id = down_token_id

    def _get_token_id(self, side: str) -> Optional[str]:
        return self._up_token_id if side == 'UP' else self._down_token_id

    # -- CLOB client init (live only) -----------------------------------------
    def _init_client(self):
        missing = [k for k, v in {
            'POLY_API_KEY':        self._api_key,
            'POLY_API_SECRET':     self._api_secret,
            'POLY_API_PASSPHRASE': self._api_passphrase,
            'POLY_WALLET_ADDRESS': self._wallet_address,
            'POLY_PRIVATE_KEY':    self._private_key,
        }.items() if not v]

        if missing:
            print('[LiveExecutor] WARNING: Missing .env variables: ' + ', '.join(missing))
            print('[LiveExecutor] Falling back to PAPER MODE -- configure keys to enable live trading.')
            self.live = False
            return

        # ── MONKEY-PATCH py_clob_client HTTP layer ──────────────────────────
        # The library uses a single httpx.Client(http2=True) module-level
        # singleton.  Long-running bots hit stale HTTP/2 connection errors
        # ("Request exception!").  We replace the request() function with a
        # robust version that:
        #   1. Uses a fresh httpx.Client with proper timeouts
        #   2. Retries transient network errors up to 3×
        #   3. Logs the REAL error (not the generic "Request exception!")
        # ────────────────────────────────────────────────────────────────────
        import py_clob_client.http_helpers.helpers as _helpers
        import httpx
        from py_clob_client.exceptions import PolyApiException

        # Replace the module-level HTTP client with a properly configured one
        _helpers._http_client.close()
        _helpers._http_client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5,
                                keepalive_expiry=30),
        )

        _MAX_RETRIES = 3
        _RETRY_DELAY = 0.5  # seconds between retries

        def robust_request(endpoint: str, method: str, headers=None, data=None):
            """Drop-in replacement for py_clob_client.http_helpers.helpers.request
            with retry logic and detailed error logging."""
            import time as _time

            last_err = None
            for attempt in range(1, _MAX_RETRIES + 1):
                try:
                    headers = _helpers.overloadHeaders(method, headers)
                    client = _helpers._http_client

                    if isinstance(data, str):
                        resp = client.request(
                            method=method, url=endpoint, headers=headers,
                            content=data.encode("utf-8"),
                        )
                    else:
                        resp = client.request(
                            method=method, url=endpoint, headers=headers,
                            json=data,
                        )

                    if resp.status_code != 200:
                        err_text = resp.text[:500]
                        print(f"[HTTP] {method} {endpoint.split('/')[-1]} → {resp.status_code}: {err_text}")
                        raise PolyApiException(resp)

                    try:
                        return resp.json()
                    except ValueError:
                        return resp.text

                except httpx.RequestError as e:
                    last_err = e
                    print(f"[HTTP] {method} {endpoint.split('/')[-1]} attempt {attempt}/{_MAX_RETRIES} "
                          f"FAILED: {type(e).__name__}: {e}")
                    if attempt < _MAX_RETRIES:
                        # Replace client — the HTTP/2 connection may be broken
                        try:
                            _helpers._http_client.close()
                        except Exception:
                            pass
                        _helpers._http_client = httpx.Client(
                            http2=True,
                            timeout=httpx.Timeout(connect=5.0, read=5.0,
                                                  write=5.0, pool=5.0),
                            limits=httpx.Limits(max_connections=20,
                                                max_keepalive_connections=5,
                                                keepalive_expiry=30),
                        )
                        _time.sleep(_RETRY_DELAY * attempt)
                    continue

            # All retries exhausted
            print(f"[HTTP] {method} FAILED after {_MAX_RETRIES} retries: {last_err}")
            raise PolyApiException(
                error_msg=f"Request exception after {_MAX_RETRIES} retries: {last_err}"
            )

        _helpers.request = robust_request
        print("[LiveExecutor] Patched HTTP layer with retry logic + fresh connections")

        # Dedicated 2-thread pool for order signing + HTTP POST
        # Keeps critical order path isolated from all other executor tasks.
        from concurrent.futures import ThreadPoolExecutor
        self._order_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix='clob-order')
        self._settle_executor = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix='clob-settle')
        print("[LiveExecutor] Dedicated executors ready (order=4, settle=3)")

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON

        creds = ApiCreds(
            api_key=self._api_key,
            api_secret=self._api_secret,
            api_passphrase=self._api_passphrase,
        )
        self._client = ClobClient(
            host='https://clob.polymarket.com',
            chain_id=POLYGON,
            key=self._private_key,
            funder=self._wallet_address,
            creds=creds,
            signature_type=1,  # POLY_PROXY: sign with key, settle from funder wallet
        )

        # Wipe credentials from memory immediately after passing to client
        self._api_key = self._api_secret = self._api_passphrase = ''
        self._private_key = ''

        # Retry initial connection check — CLOB sometimes returns 425 "service not ready"
        for attempt in range(1, 6):
            try:
                ok = self._client.get_ok()
                print(f'[LiveExecutor] LIVE MODE connected. Server: {ok}')
                break
            except Exception as e:
                safe_msg = str(e).replace(self._wallet_address, '0x***') if self._wallet_address else str(e)
                if attempt < 5:
                    wait = 3 * attempt
                    print(f'[LiveExecutor] CLOB connection attempt {attempt}/5 failed ({safe_msg[:100]}) — retry in {wait}s')
                    time.sleep(wait)
                else:
                    raise ConnectionError(f'[LiveExecutor] CLOB connection failed after 5 attempts: {safe_msg}')

    # -- simulate_buy: paper + optional live -----------------------------------
    async def simulate_buy(self, side: str, price: float, qty: float,
                           orderbook: dict = None,
                           time_remaining_s: float = None) -> FillResult:
        # Always run paper sim first (keeps stats and UI working)
        paper = self._sim.simulate_fill(side, price, qty, orderbook or {})

        if not self.live:
            return paper

        # Hard ban: no new BUY orders within 30s of market close
        if time_remaining_s is not None and time_remaining_s < 30:
            blocked = copy.copy(paper)
            blocked.filled = False
            blocked.reason = 'BUY_BLOCKED_TIME_LIMIT'
            print(f'[LIVE] BUY {side} BLOCKED — only {time_remaining_s:.0f}s left in window')
            return blocked

        usd = min(price * qty, MAX_SINGLE_ORDER_USD)
        token_id = self._get_token_id(side)

        if not token_id:
            logger.warning('[LiveExecutor] No token_id for %s -- skip live order', side)
            blocked = copy.copy(paper)
            blocked.filled = False
            blocked.reason = 'BUY_NO_TOKEN_ID'
            return blocked

        if self._open_exposure + usd > MAX_OPEN_EXPOSURE_USD:
            print(f'[LiveExecutor] ⛔ Exposure cap ${self._open_exposure:.2f}/${MAX_OPEN_EXPOSURE_USD:.0f} — blocking BUY {side}')
            blocked = copy.copy(paper)
            blocked.filled = False
            blocked.reason = 'BUY_EXPOSURE_CAP'
            return blocked

        # Cooldown and buy count limits removed — strategy logic handles
        # rung gating (no new buys while unsold rungs exist).
        # In-flight guard below prevents concurrent duplicate BUYs.
        now = time.time()

        # In-flight guard: prevent two concurrent BUY orders for the same side.
        # asyncio is single-threaded so no actual lock needed — set membership
        # is checked and updated between awaits (cooperative multitasking).
        if side in self._buy_in_flight:
            print(f'[LiveExecutor] ⛔ BUY {side} already in-flight — skipping duplicate ENTRY signal')
            blocked = copy.copy(paper)
            blocked.filled = False
            blocked.reason = 'BUY_IN_FLIGHT'
            return blocked

        self._buy_in_flight.add(side)
        try:
            result = await self._place_order('BUY', side, token_id, price, qty, usd, paper)
        finally:
            self._buy_in_flight.discard(side)

        if result.filled:
            self._open_exposure += result.total_cost
            self._token_position_qty[token_id] = self._token_position_qty.get(token_id, 0.0) + result.filled_qty
            self._token_position_cost[token_id] = self._token_position_cost.get(token_id, 0.0) + result.total_cost
            self._last_buy_time[side] = now
            print(f'[LiveExecutor] 💰 BUY {side}: ${result.total_cost:.2f} | exposure ${self._open_exposure:.2f}/{MAX_OPEN_EXPOSURE_USD:.0f}')
            # Mark token as NOT yet sellable — settlement poller will confirm
            # when CLOB sees the on-chain balance (typically 5–50s after BUY fill).
            self._settlement_ready[token_id] = False
            self._settlement_confirmed_real[token_id] = False  # reset for new rung
            self._settlement_created[token_id] = time.time()
            # Aggressive settlement priming — hammer update_balance_allowance
            # multiple times to minimize settlement delay. Each call pings
            # CLOB to re-scan the chain for our tokens.
            async def _prime_settlement(tid):
                for _i in range(15):
                    await self._async_update_balance_allowance(tid)
                    await asyncio.sleep(0.2)
            asyncio.ensure_future(_prime_settlement(token_id))
            self._ensure_poller_running()
        return result

    # -- simulate_sell: paper + optional live ----------------------------------
    async def simulate_sell(self, side: str, price: float, qty: float,
                            orderbook: dict = None, bid_price: float = None,
                            stop_loss: bool = False) -> FillResult:
        paper = self._sim.simulate_fill(side, price, qty, orderbook or {})

        if not self.live:
            return paper

        # Start CLOB keepalive on first live call
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.ensure_future(self._clob_keepalive())

        token_id = self._get_token_id(side)
        if not token_id:
            return paper

        # No settlement gate — always attempt SELL immediately.
        # If CLOB doesn't have the shares yet, we get NO_BALANCE_ALLOWANCE
        # which is handled below. Never block a sell attempt.

        # GTC SL disabled — no pending orders to cancel.

        # GTC SL disabled — Polymarket limit sells fill instantly when
        # price > SL, which sells at wrong time. FAK-only for all sells.

        usd = price * qty
        result = await self._place_order('SELL', side, token_id, price, qty, usd, paper, bid_price=bid_price, stop_loss=stop_loss)

        if result.filled:
            tracked_qty = self._token_position_qty.get(token_id, 0.0)
            tracked_cost = self._token_position_cost.get(token_id, 0.0)

            if tracked_qty > 0 and tracked_cost > 0:
                sold_qty = min(result.filled_qty, tracked_qty)
                avg_cost = tracked_cost / tracked_qty
                released_cost = min(tracked_cost, avg_cost * sold_qty)
                self._token_position_qty[token_id] = max(0.0, tracked_qty - sold_qty)
                self._token_position_cost[token_id] = max(0.0, tracked_cost - released_cost)
                self._open_exposure = max(0.0, self._open_exposure - released_cost)
            else:
                released_cost = min(result.total_cost, self._open_exposure)
                self._open_exposure = max(0.0, self._open_exposure - released_cost)
                logger.warning('[LiveExecutor] Missing cost basis for %s — fallback release $%.2f',
                               token_id[:20], released_cost)

            # Re-trigger CLOB scan so subsequent sells see updated balance
            asyncio.ensure_future(self._async_update_balance_allowance(token_id))

        elif getattr(result, 'reason', '') and 'NO_BALANCE_ALLOWANCE' in result.reason:
            # CLOB still doesn't see the tokens — restart poller
            self._settlement_ready[token_id] = False
            if token_id not in self._settlement_created:
                self._settlement_created[token_id] = time.time()
            self._ensure_poller_running()

        return result

    # -- NOTE: ALL BUY fills (maker & taker) settle on-chain — conditional
    #    tokens are minted on Polygon and CLOB needs update_balance_allowance
    #    to discover them.  Maker-only BUY avoids the 10% taker fee but still
    #    requires the same chain settlement wait before SELL.

    # Timing settings for limit orders — keep short for 5-min markets
    BUY_LIMIT_TIMEOUT_S  = 4    # cancel BUY if unfilled after 4s → skip trade
    SELL_LIMIT_TIMEOUT_S = 1    # cancel SELL if unfilled after 1s — faster fallback to taker → fallback to taker
    MIN_ORDER_SIZE       = 5.0  # Polymarket minimum shares per order

    # -- Internal: place order on CLOB ----------------------------------------
    BID_FALLBACK_FLOOR = 0.05  # minimum bid price for emergency fallback sell

    async def _place_order(self, action: str, side: str, token_id: str,
                           price: float, qty: float, usd: float,
                           paper: FillResult, bid_price: float = None,
                           stop_loss: bool = False) -> FillResult:
        t0 = time.time()

        if action == 'BUY':
            size = round(usd / price, 2) if price > 0 else round(qty, 2)  # maker: exact shares
            size = max(size, self.MIN_ORDER_SIZE)
            loop = asyncio.get_running_loop()

            # Maker BUY: post GTC @ask-1tick, retry with fresh price if not filled
            for _buy_attempt in range(5):
                # Fetch current best ask from orderbook
                _maker_px = round(price - 0.01, 2)  # default: strategy price - 1 tick
                try:
                    book = await loop.run_in_executor(
                        self._order_executor, lambda: self._client.get_order_book(token_id))
                    raw_asks = getattr(book, 'asks', None) or []
                    if raw_asks:
                        # Sort ascending — best (lowest) ask first
                        def _ask_px(x):
                            return float(x['price'] if isinstance(x, dict) else getattr(x, 'price', 99))
                        sorted_asks = sorted(raw_asks, key=_ask_px)
                        _best_ask = _ask_px(sorted_asks[0])
                        _maker_px = round(_best_ask - 0.01, 2)
                        print(f'[LIVE] BUY {side} orderbook: best_ask={_best_ask:.2f} → maker@{_maker_px:.2f}')
                except Exception:
                    pass
                _maker_px = max(0.02, min(0.98, _maker_px))
                size = round(usd / _maker_px, 2) if _maker_px > 0 else size  # recalculate for new price

                print(f'[LIVE] BUY {side} maker attempt {_buy_attempt+1}/5 @ {_maker_px:.2f} size={size:.2f}')
                result = await self._place_limit_order(
                    action='BUY', side=side, token_id=token_id,
                    price=_maker_px, size=size,
                    timeout_s=1.0,  # 1s per attempt
                    paper=paper, t0=t0,
                    post_only=True,
                    use_fak=False,
                )
                if result.filled:
                    return result
                # Check if it was crosses-book error — price moved, retry
                if 'MAKER_ONLY_FAILED' in getattr(result, 'reason', ''):
                    continue  # retry with fresh orderbook
                if 'LIMIT_TIMEOUT' in getattr(result, 'reason', ''):
                    continue  # didn't fill in time, retry with fresh price

            # All maker attempts failed — return unfilled, strategy retries next tick
            print(f'[LIVE] BUY {side} maker failed 5x — no taker fallback')
            failed = copy.copy(paper)
            failed.filled = False
            failed.reason = 'BUY_MAKER_FAILED'
            return failed

        else:  # SELL
            # ── Wait for settlement before selling ───────────────────────
            # After FAK BUY, shares take 3-5s to settle on-chain.
            # Poll CLOB balance until shares are visible.
            #
            # Smart exit: if we PREVIOUSLY had confirmed shares on this
            # token (tracked in _token_position_qty) but CLOB now shows 0,
            # the market was resolved/redeemed — stop waiting immediately.
            # If we NEVER had confirmed shares, keep waiting (fresh buy).
            _settle_waited = 0.0
            _clob_bal = 0.0
            _poll_interval = 0.20
            _logged_waiting = False
            # Check if settlement has already been confirmed for this token.
            # If yes and CLOB=0, the market truly resolved (shares redeemed).
            # If no (fresh buy, settlement pending), keep waiting.
            _settlement_was_confirmed = self._settlement_confirmed_real.get(token_id, False)
            while _clob_bal < 0.5:
                try:
                    _clob_bal = await self._async_get_balance(token_id)
                except Exception as _e:
                    logger.warning('[LIVE] SELL balance poll: %s', _e)
                    _clob_bal = 0.0
                if _clob_bal > 0.5:
                    break
                # After 8s with CLOB=0, force-sell regardless.
                # This covers both unsettled shares AND stale CLOB.
                # If market is truly resolved, Polymarket will reject
                # and strategy handles the reconciliation.
                if _settle_waited > 8.0:
                    _force_qty = self._token_position_qty.get(token_id, qty)
                    _force_size = int(_force_qty * 100) / 100
                    if _force_size >= 0.5:
                        print(f'[LIVE] SELL {side} FORCE SELL after {_settle_waited:.0f}s — trying {_force_size:.2f} shares despite CLOB=0')
                        qty = _force_qty
                        _clob_bal = _force_qty
                        break
                    # Nothing to force-sell — truly resolved
                    if _settlement_was_confirmed:
                        print(f'[LIVE] SELL {side} market resolved — no shares after {_settle_waited:.0f}s')
                        failed = copy.copy(paper)
                        failed.filled = False
                        failed.reason = 'SELL_MARKET_RESOLVED'
                        return failed
                    break
                if not _logged_waiting:
                    print(f'[LIVE] SELL {side} waiting for settlement (CLOB=0)...')
                    _logged_waiting = True
                if _settle_waited > 10.0:
                    await asyncio.sleep(1.0)
                    _settle_waited += 1.0
                else:
                    await asyncio.sleep(_poll_interval)
                    _settle_waited += _poll_interval
            if _settle_waited > 0:
                print(f'[LIVE] SELL {side} settlement confirmed after {_settle_waited:.1f}s (CLOB={_clob_bal:.2f})')
            # Always sell ENTIRE CLOB balance — prevents dust accumulation.
            # Paper qty may differ from CLOB due to fees, but we always
            # want to exit the full position, never leave crumbs.
            if abs(_clob_bal - qty) > 0.01:
                print(f'[LIVE] SELL {side} using CLOB balance: {_clob_bal:.4f} (paper={qty:.2f})')
            qty = _clob_bal
            size = int(qty * 100) / 100  # floor to 2 decimals — Polymarket max
            if size < 0.5:
                print(f'[LiveExecutor] SELL {side} qty={size:.2f} — dust, skipping')
                dust = copy.copy(paper)
                dust.filled = False
                dust.reason = f'DUST qty={size:.2f}'
                return dust
            if stop_loss:
                # Cancel any pending maker TP order — we're exiting at SL
                if token_id in self._pending_tp_order:
                    await self.cancel_pending_tp(side)
                # STOP sells: FAK taker at bid-1tick for immediate exit
                _stop_px = (max(self.BID_FALLBACK_FLOOR, round(bid_price - 0.01, 2))  # -1 tick
                            if bid_price and bid_price >= self.BID_FALLBACK_FLOOR
                            else price)
                print(f'[LIVE] SELL {side} STOP — crossing bid-1tick @ {_stop_px:.4f} (immediate taker)')
                result = await self._place_limit_order(
                    action='SELL', side=side, token_id=token_id,
                    price=_stop_px, size=size,
                    timeout_s=5.0,
                    paper=paper, t0=t0,
                    post_only=False,
                    use_fak=True,
                )
            else:
                # Profit-sell: check if maker TP order already filled
                if token_id in self._pending_tp_order:
                    tp_info = self._pending_tp_order[token_id]
                    _oid = tp_info.get('order_id', '')
                    if _oid:
                        try:
                            loop = asyncio.get_running_loop()
                            order_info = await loop.run_in_executor(
                                self._order_executor, lambda: self._client.get_order(_oid))
                            if order_info:
                                _st = str(order_info.get('status', '')).lower()
                                _sm = float(order_info.get('size_matched') or 0)
                                if _st == 'matched' or _sm >= tp_info['qty'] * 0.9:
                                    _tp_price = float(order_info.get('avg_price') or tp_info['price'])
                                    self._pending_tp_order.pop(token_id, None)
                                    print(f'[LIVE] Maker TP FILLED for {side} — {_sm:.2f} shares @ ${_tp_price:.3f} (0% fee)')
                                    result = copy.copy(paper)
                                    result.filled = True
                                    result.fill_price = _tp_price
                                    result.filled_qty = _sm
                                    result.total_cost = _sm * _tp_price
                                    result.reason = f'MAKER_TP_FILLED_{side}'
                                    tracked_qty = self._token_position_qty.get(token_id, 0.0)
                                    if tracked_qty > 0:
                                        released = min(self._token_position_cost.get(token_id, 0.0), self._open_exposure)
                                        self._token_position_qty[token_id] = 0.0
                                        self._token_position_cost[token_id] = 0.0
                                        self._open_exposure = max(0.0, self._open_exposure - released)
                                    return result
                        except Exception as _e:
                            print(f'[LIVE] Maker TP status check failed: {_e}')
                    # TP order not filled yet — cancel and use FAK as fallback
                    await self.cancel_pending_tp(side)
                # FAK taker sell for TP
                _sell_px = (max(self.BID_FALLBACK_FLOOR, round(bid_price - 0.01, 2))
                            if bid_price and bid_price >= self.BID_FALLBACK_FLOOR
                            else price)
                print(f'[LIVE] SELL {side} PROFIT — FAK taker @ {_sell_px:.4f}')
                result = await self._place_limit_order(
                    action='SELL', side=side, token_id=token_id,
                    price=_sell_px, size=size,
                    timeout_s=self.SELL_LIMIT_TIMEOUT_S,
                    paper=paper, t0=t0,
                    post_only=False,
                    use_fak=True,
                )
            # Aggressive retry: if FAK sell failed, hammer with rapid retries
            # at progressively lower prices until filled or book confirmed empty
            if not result.filled and bid_price and bid_price >= self.BID_FALLBACK_FLOOR:
                if getattr(result, 'reason', '') == 'SELL_FAK_EMPTY_BOOK':
                    pass  # book empty — strategy retries next tick (~20ms)
                else:
                    # Rapid retry loop: up to 3 attempts, 0ms delay, -1 tick each
                    for _retry in range(3):
                        _retry_px = max(self.BID_FALLBACK_FLOOR, round(bid_price - 0.01 * (_retry + 1), 2))
                        print(f'[LIVE] SELL {side} — aggressive retry #{_retry+1} @ {_retry_px:.4f}')
                        result = await self._place_limit_order(
                            action='SELL', side=side, token_id=token_id,
                            price=_retry_px, size=size,
                            timeout_s=0.5, paper=paper, t0=t0,
                            post_only=False, use_fak=True)
                        if result.filled or getattr(result, 'reason', '') == 'SELL_FAK_EMPTY_BOOK':
                            break
                if not result.filled and getattr(result, 'reason', '') != 'SELL_FAK_EMPTY_BOOK':
                    # Final emergency: cross at floor price
                    print(f'[LIVE] SELL {side} — emergency floor @ {bid_price:.4f}')
                    result = await self._place_limit_order(
                        action='SELL', side=side, token_id=token_id,
                        price=bid_price, size=size,
                        timeout_s=2.0,
                        paper=paper, t0=t0,
                        post_only=False,
                    )
            return result

    # Known market parameters for btc-updown (avoids CLOB API lookups)
    _TICK_SIZE  = '0.01'
    _NEG_RISK   = False
    _FEE_RATE   = 1000   # 10% fee on btc-updown markets

    async def _place_limit_order(self, action: str, side: str, token_id: str,
                                price: float, size: float, timeout_s: float,
                                paper: FillResult, t0: float,
                                post_only: bool = True,
                                use_fak: bool = False) -> FillResult:
        """Place a GTC limit order and poll until filled or timeout.

        All HTTP calls run in a thread-pool executor so the event loop is never
        blocked.  The poll loop uses asyncio.sleep (non-blocking).
        """
        from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
        from py_clob_client.order_builder.constants import BUY as _BUY, SELL as _SELL

        loop = asyncio.get_running_loop()
        clob_side = _BUY if action == 'BUY' else _SELL
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=size,
            side=clob_side,
            fee_rate_bps=self._FEE_RATE,
        )
        opts = CreateOrderOptions(tick_size=self._TICK_SIZE, neg_risk=self._NEG_RISK)

        resp = None
        order_id = None
        try:
            _exec = self._order_executor  # dedicated pool: sign + POST never share threads
            signed_order = await loop.run_in_executor(
                _exec, lambda: self._client.builder.create_order(order_args, opts))
            _otype = OrderType.FAK if use_fak else OrderType.GTC
            _po = False if use_fak else post_only
            resp = await loop.run_in_executor(
                _exec, lambda: self._client.post_order(signed_order, _otype, post_only=_po))
            print(f'[LIVE] {action} {side} type={"FAK" if use_fak else "GTC"} post_only={_po} → {resp}')
        except Exception as e:
            safe_msg = str(e)
            if self._wallet_address:
                safe_msg = safe_msg.replace(self._wallet_address, '0x***')
            print(f'[LIVE ERROR] {action} {side} post_only={post_only}: {safe_msg[:300]}')
            logger.warning('[LiveExecutor] %s %s post_only=%s failed: %s', action, side, post_only, safe_msg[:500])

            if 'not enough balance / allowance' in safe_msg.lower():
                # Tokens not yet visible — return immediately; settlement poller
                # will re-confirm and next SELL attempt will proceed.
                failed = copy.copy(paper)
                failed.filled = False
                failed.reason = f'{action}_NO_BALANCE_ALLOWANCE'
                return failed

            if 'does not exist' in safe_msg.lower():
                failed = copy.copy(paper)
                failed.filled = False
                failed.reason = f'{action}_ORDERBOOK_EXPIRED'
                print(f'[LIVE] {action} {side} orderbook expired — skipping')
                return failed

            if post_only:
                if action == 'BUY':
                    # One-shot: fetch live order book and place at best_ask - 0.01.
                    # Abort if the gap from original price is > 0.03 (signal is stale).
                    try:
                        book = await loop.run_in_executor(
                            None, lambda: self._client.get_order_book(token_id))
                        raw_asks = getattr(book, 'asks', None) or []
                        def _px(x):
                            return float(x['price'] if isinstance(x, dict) else getattr(x, 'price', 99))
                        asks_sorted = sorted(raw_asks, key=_px)
                        if asks_sorted:
                            best_ask = _px(asks_sorted[0])
                            maker_px = round(best_ask - 0.01, 4)
                            gap = round(price - maker_px, 4)
                            if maker_px >= 0.02 and gap <= 0.03:
                                print(f'[LIVE] BUY {side} crossed book — one-shot maker @ {maker_px:.4f} (ask={best_ask:.4f})')
                                return await self._place_limit_order(
                                    action, side, token_id, maker_px, size, timeout_s, paper, t0, post_only=True)
                            else:
                                print(f'[LIVE] BUY {side} crossed book (gap={gap:.4f}) — signal stale, skipping')
                    except Exception as book_err:
                        logger.warning('[LiveExecutor] orderbook fetch for cross-book retry: %s', book_err)
                    failed = copy.copy(paper)
                    failed.filled = False
                    failed.reason = f'{action}_MAKER_ONLY_FAILED'
                    return failed
                else:
                    print(f'[LIVE] SELL {side} post_only failed — retrying as taker LIMIT')
                    return await self._place_limit_order(
                        action, side, token_id, price, size, timeout_s, paper, t0, post_only=False)

            # FAK "no orders found" = book is empty. Return immediately so the
            # strategy can retry next tick (~50ms) with a fresh bid price,
            # rather than wasting seconds retrying against an empty book.
            if use_fak and 'no orders found' in safe_msg.lower():
                _nf2 = copy.copy(paper); _nf2.filled = False
                _nf2.reason = f'{action}_FAK_EMPTY_BOOK'
                print(f'[LIVE] {action} {side} FAK book empty — returning for fast retry')
                return _nf2

            failed = copy.copy(paper)
            failed.filled = False
            failed.reason = f'{action}_LIMIT_FAILED'
            return failed

        # FAK (Fill-and-Kill) orders are resolved from the initial response — no polling
        if use_fak:
            _take = float(resp.get('takingAmount') or 0)
            _make = float(resp.get('makingAmount') or 0)
            if _take > 0 or _make > 0:
                # Polymarket CLOB FAK response semantics:
                # BUY:  takingAmount = shares received, makingAmount = USDC spent
                # SELL: takingAmount = USDC received,   makingAmount = shares given
                # Verified against actual API responses.
                if action == 'BUY':
                    _sh_est = _take   # shares we received
                    _us = _make       # USDC we spent
                else:
                    _us = _take       # USDC we received
                    _sh_est = _make   # shares we gave

                # BUY: use FAK takingAmount as filled_qty.
                # This is pre-fee but the ONLY reliable per-trade value.
                # CLOB absolute balance includes dust from previous trades
                # and can't be used to determine THIS trade's fill.
                # At SELL time, CLOB balance is polled for exact sellable qty.
                _sh = _sh_est

                # Price = USDC / shares, capped at $1.00 (binary outcome)
                _fp = min(_us / _sh, 1.0) if _sh > 0 else price
                _lat = (time.time() - t0) * 1000
                _oid = resp.get('orderID') or resp.get('id') or 'fak'
                r = copy.copy(paper)
                r.filled = True; r.fill_price = round(_fp, 6)
                r.filled_qty = round(_sh, 6); r.total_cost = round(_us, 6)
                r.latency_ms = _lat
                r.reason = f'LIVE {action} FAK filled id={_oid}'
                print(f'[LIVE] {action} {side} FAK FILLED: {_sh:.4f} @ {_fp:.4f} ${_us:.2f} {_lat:.0f}ms')
                return r
            _lat = (time.time() - t0) * 1000
            print(f'[LIVE] {action} {side} FAK no fill {_lat:.0f}ms — status={resp.get("status", "?")}')
            _nf = copy.copy(paper); _nf.filled = False
            _nf.reason = f'{action}_FAK_NO_FILL'
            return _nf

        order_id = resp.get('orderID') or resp.get('id') or ''
        if not order_id or order_id == '?':
            print(f'[LIVE ERROR] {action} {side} — no orderID in response: {resp}')
            failed = copy.copy(paper)
            failed.filled = False
            failed.reason = f'{action}_NO_ORDER_ID'
            return failed

        # Fast path: immediate match
        try:
            if str(resp.get('status', '')).lower() == 'matched':
                take_amt = float(resp.get('takingAmount') or 0)
                make_amt = float(resp.get('makingAmount') or 0)
                if take_amt > 0 and make_amt > 0:
                    # Deterministic: BUY taking=shares making=USDC
                    #                SELL taking=USDC   making=shares
                    if action == 'BUY':
                        shares_amt, usdc_amt = take_amt, make_amt
                    else:
                        usdc_amt, shares_amt = take_amt, make_amt
                    fill_price = min(usdc_amt / shares_amt, 1.0) if shares_amt > 0 else price
                    latency_ms = (time.time() - t0) * 1000
                    live_result            = copy.copy(paper)
                    live_result.filled     = True
                    live_result.fill_price = round(fill_price, 6)
                    live_result.filled_qty = round(shares_amt, 6)
                    live_result.total_cost = round(usdc_amt, 6)
                    live_result.latency_ms = latency_ms
                    live_result.reason     = f'LIVE {action} LIMIT matched-immediate id={order_id}'
                    print(f'[LIVE] {action} {side} MATCHED IMMEDIATE: {shares_amt:.4f} @ {fill_price:.4f} ${usdc_amt:.2f} {latency_ms:.0f}ms')
                    return live_result
        except Exception as parse_err:
            logger.warning('[LiveExecutor] immediate match parse failed: %s', parse_err)

        print(f'[LIVE] {action} {side} polling id={order_id} price={price:.4f} size={size:.2f} (up to {timeout_s:.0f}s)')

        # ── Poll until filled or timeout — non-blocking ───────────────────────
        deadline = time.time() + timeout_s
        _first_poll = True
        while time.time() < deadline:
            await asyncio.sleep(0.3 if _first_poll else 0.5)
            _first_poll = False
            try:
                order_info = await loop.run_in_executor(
                    self._order_executor, lambda: self._client.get_order(order_id))
                if not order_info:
                    continue
                status       = order_info.get('status', '')
                size_matched = float(order_info.get('size_matched') or
                                     order_info.get('sizeMatched') or
                                     order_info.get('matched_amount') or 0)
                fill_price   = float(order_info.get('avg_price') or
                                     order_info.get('price') or price)

                if status in ('matched',) or size_matched >= size * 0.99:
                    latency_ms = (time.time() - t0) * 1000
                    total_cost = round(fill_price * size_matched, 4)
                    print(f'[LIVE] {action} {side} FILLED: {size_matched:.2f} @ {fill_price:.4f} ${total_cost:.2f} {latency_ms:.0f}ms')
                    live_result            = copy.copy(paper)
                    live_result.filled     = True
                    live_result.fill_price = fill_price
                    live_result.filled_qty = size_matched
                    live_result.total_cost = total_cost
                    live_result.latency_ms = latency_ms
                    live_result.reason     = f'LIVE {action} LIMIT id={order_id}'
                    return live_result

                if status in ('cancelled', 'unmatched'):
                    print(f'[LIVE] {action} {side} order {status} id={order_id}')
                    if size_matched > 0.1:
                        latency_ms = (time.time() - t0) * 1000
                        total_cost = round(fill_price * size_matched, 4)
                        live_result            = copy.copy(paper)
                        live_result.filled     = True
                        live_result.fill_price = fill_price
                        live_result.filled_qty = size_matched
                        live_result.total_cost = total_cost
                        live_result.latency_ms = latency_ms
                        live_result.reason     = f'LIVE {action} LIMIT partial id={order_id}'
                        return live_result
                    failed = copy.copy(paper)
                    failed.filled = False
                    failed.reason = f'{action}_ORDER_{status.upper()} id={order_id}'
                    return failed

                elapsed = time.time() - t0
                print(f'[LIVE] {action} {side} waiting… matched={size_matched:.2f}/{size:.2f} status={status} {elapsed:.0f}s')

            except Exception as poll_err:
                logger.warning('[LiveExecutor] poll error: %s', poll_err)

        # ── Timeout ───────────────────────────────────────────────────────────
        print(f'[LIVE] {action} {side} TIMEOUT {timeout_s:.0f}s — cancelling {order_id}')
        try:
            await loop.run_in_executor(None, lambda: self._client.cancel(order_id))
        except Exception as cancel_err:
            logger.warning('[LiveExecutor] cancel failed: %s', cancel_err)

        # Check twice for late fills — CLOB cancel is async and a match can
        # land between the cancel request and our status check.
        for _post_cancel_attempt in range(2):
            try:
                if _post_cancel_attempt > 0:
                    await asyncio.sleep(0.3)  # brief wait for CLOB to settle
                order_info   = await loop.run_in_executor(
                    self._order_executor, lambda: self._client.get_order(order_id))
                size_matched = float(order_info.get('size_matched') or order_info.get('sizeMatched') or 0)
                if size_matched > 0.1:
                    fill_price = float(order_info.get('avg_price') or order_info.get('price') or price)
                    latency_ms = (time.time() - t0) * 1000
                    total_cost = round(fill_price * size_matched, 4)
                    live_result            = copy.copy(paper)
                    live_result.filled     = True
                    live_result.fill_price = fill_price
                    live_result.filled_qty = size_matched
                    live_result.total_cost = total_cost
                    live_result.latency_ms = latency_ms
                    live_result.reason     = f'LIVE {action} LIMIT post-cancel-fill id={order_id}'
                    print(f'[LIVE] {action} {side} POST-CANCEL FILL detected: {size_matched:.2f} @ {fill_price:.4f} ${total_cost:.2f}')
                    return live_result
            except Exception:
                pass

        timed_out         = copy.copy(paper)
        timed_out.filled  = False
        timed_out.reason  = f'LIMIT_TIMEOUT id={order_id}'
        return timed_out

    # -- Settlement poller (continuous background async task) ------------------
    async def _clob_keepalive(self) -> None:
        """Ping CLOB every 25 s — prevents Cloudflare idle-connection close.
        Without this, the first order after >30 s idle pays a 50–100 ms
        TLS re-handshake penalty at the worst possible moment.
        """
        while self.live:
            await asyncio.sleep(25)
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._client.get_ok)
            except Exception:
                pass

    async def post_maker_tp(self, side: str, tp_price: float, qty: float):
        """Post a maker limit sell at the TP price after settlement.
        The order sits in the book above current price. When price
        rises to TP, it fills as maker (0% fee)."""
        if not self.live or not self._client:
            return
        token_id = self._get_token_id(side)
        if not token_id:
            return
        # Get actual CLOB balance for qty
        try:
            _clob_bal = await self._async_get_balance(token_id)
            if _clob_bal > 0.5:
                qty = _clob_bal
        except Exception:
            pass
        size = int(qty * 100) / 100
        if size < 0.5:
            return
        from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
        from py_clob_client.order_builder.constants import SELL as _SELL
        loop = asyncio.get_running_loop()
        order_args = OrderArgs(
            token_id=token_id,
            price=round(tp_price, 4),
            size=size,
            side=_SELL,
            fee_rate_bps=self._FEE_RATE,
        )
        opts = CreateOrderOptions(tick_size=self._TICK_SIZE, neg_risk=self._NEG_RISK)
        try:
            _exec = self._order_executor
            signed_order = await loop.run_in_executor(
                _exec, lambda: self._client.builder.create_order(order_args, opts))
            resp = await loop.run_in_executor(
                _exec, lambda: self._client.post_order(signed_order, OrderType.GTC))
            order_id = resp.get('orderID') or resp.get('id') or ''
            if order_id:
                # Check if it matched immediately (price already below SL)
                _resp_status = str(resp.get('status', '')).lower()
                _resp_take = float(resp.get('takingAmount') or 0)
                _resp_make = float(resp.get('makingAmount') or 0)
                if _resp_status == 'matched' and _resp_take > 0:
                    # GTC SL filled instantly — shares are gone
                    _fill_px = _resp_take / _resp_make if _resp_make > 0 else tp_price
                    print(f'[LIVE] GTC SL INSTANT FILL: SELL {side} {_resp_make:.2f}@{_fill_px:.3f} (posted@{tp_price:.3f})')
                    # Update tracking — shares sold
                    tracked_qty = self._token_position_qty.get(token_id, 0.0)
                    if tracked_qty > 0:
                        released = min(self._token_position_cost.get(token_id, 0.0), self._open_exposure)
                        self._token_position_qty[token_id] = 0.0
                        self._token_position_cost[token_id] = 0.0
                        self._open_exposure = max(0.0, self._open_exposure - released)
                    # Don't store as pending — already filled
                    return
                self._pending_tp_order[token_id] = {
                    'order_id': order_id, 'price': tp_price,
                    'qty': size, 'side': side
                }
                print(f'[LIVE] Maker TP posted: SELL {side} {size:.1f}@{tp_price:.3f} id={order_id[:20]} status={_resp_status}')
            else:
                print(f'[LIVE] Maker TP post failed — no order_id: {resp}')
        except Exception as e:
            print(f'[LIVE] Maker TP post error: {str(e)[:200]}')

    async def cancel_pending_tp(self, side: str) -> bool:
        """Cancel pending GTC SL order for a side. Returns True if cancelled.
        If the order already filled, updates tracking and returns False."""
        token_id = self._get_token_id(side)
        if not token_id or token_id not in self._pending_tp_order:
            return True  # nothing to cancel
        info = self._pending_tp_order.pop(token_id)
        order_id = info['order_id']
        try:
            # First check if it already filled
            loop = asyncio.get_running_loop()
            order_info = await loop.run_in_executor(
                self._order_executor, lambda: self._client.get_order(order_id))
            if order_info:
                _st = str(order_info.get('status', '')).lower()
                _sm = float(order_info.get('size_matched') or 0)
                if _st == 'matched' or _sm >= info['qty'] * 0.9:
                    # Already filled — update tracking
                    print(f'[LIVE] Maker TP already filled when cancelling: {side} {_sm:.2f} shares')
                    tracked_qty = self._token_position_qty.get(token_id, 0.0)
                    if tracked_qty > 0:
                        released = min(self._token_position_cost.get(token_id, 0.0), self._open_exposure)
                        self._token_position_qty[token_id] = 0.0
                        self._token_position_cost[token_id] = 0.0
                        self._open_exposure = max(0.0, self._open_exposure - released)
                    return False  # signal caller: don't send FAK, already sold
            # Not filled — cancel it
            await loop.run_in_executor(self._order_executor, lambda: self._client.cancel(order_id))
            print(f'[LIVE] Maker TP cancelled: {side} id={order_id[:20]}')
            return True
        except Exception as e:
            print(f'[LIVE] Maker TP cancel error: {str(e)[:100]}')
            return False

    def has_pending_tp(self, side: str) -> bool:
        token_id = self._get_token_id(side)
        return token_id in self._pending_tp_order if token_id else False

    def _ensure_poller_running(self):
        """Start the settlement poller if it isn't already running."""
        if self._poller_task is None or self._poller_task.done():
            try:
                loop = asyncio.get_event_loop()
                self._poller_task = loop.create_task(self._settlement_poller())
            except RuntimeError:
                pass  # no event loop yet — will start on next call

    # Two-tier settlement polling: fast for fresh tokens, slow for stale ones
    _SETTLE_FRESH_AGE_S    = 30.0   # tokens < 30s old = fresh (poll fast)
    _SETTLE_FRESH_INTERVAL = 0.25   # poll fresh tokens every 0.25s (unchanged)
    _SETTLE_STALE_INTERVAL = 3.0    # poll stale tokens every 3s
    _SETTLE_STALE_BATCH    = 3      # max stale tokens per round
    _SETTLE_ZOMBIE_AGE_S   = 120.0  # stop polling tokens older than 2 min

    async def _settlement_poller(self):
        """Two-tier settlement poller: fast for fresh tokens, throttled for stale.

        Fresh tokens (< 30s old): polled every 0.25s with no batch limit.
        Stale tokens (30-120s): polled every 3s, max 3 per round.
        Zombie tokens (> 120s): evicted — sell will retry via NO_BALANCE_ALLOWANCE.
        """
        print('[LiveExecutor] ⏱️ Settlement poller started (two-tier)')
        loop = asyncio.get_running_loop()
        _last_stale_poll = 0.0
        while True:
            now = time.time()
            pending = [tid for tid, ready in self._settlement_ready.items() if not ready]
            if not pending:
                break

            # Classify tokens by age
            fresh = []
            stale = []
            zombie = []
            for tid in pending:
                age = now - self._settlement_created.get(tid, now)
                if age > self._SETTLE_ZOMBIE_AGE_S:
                    zombie.append(tid)
                elif age > self._SETTLE_FRESH_AGE_S:
                    stale.append(tid)
                else:
                    fresh.append(tid)

            # Zombie tokens: DON'T evict — keep polling slowly.
            # These are real shares we paid for. Never give up.
            # Just move them to stale tier (poll every 3s instead of 0.25s).
            for tid in zombie:
                # Reset age to keep in stale tier (not evicted)
                self._settlement_created[tid] = now - self._SETTLE_FRESH_AGE_S - 1
                if not hasattr(self, '_zombie_warned'):
                    self._zombie_warned = set()
                if tid not in self._zombie_warned:
                    self._zombie_warned.add(tid)
                    print(f'[LiveExecutor] ⚠️ Settlement slow: {tid[:16]}… (> {self._SETTLE_ZOMBIE_AGE_S:.0f}s, still polling)')

            # Build this round's check list
            check_list = list(fresh)  # always check all fresh tokens
            if now - _last_stale_poll >= self._SETTLE_STALE_INTERVAL and stale:
                check_list.extend(stale[:self._SETTLE_STALE_BATCH])
                _last_stale_poll = now

            if not check_list:
                if not stale:
                    break  # nothing left to poll
                await asyncio.sleep(self._SETTLE_STALE_INTERVAL)
                continue

            async def _check_one(tid):
                try:
                    await self._async_update_balance_allowance(tid)
                    bal = await self._async_get_balance(tid)
                    expected = self._token_position_qty.get(tid, 0.0)
                    threshold = expected * 0.9 if expected > 0.1 else 0.5
                    if bal >= threshold:
                        self._settlement_ready[tid] = True
                        self._settlement_confirmed_real[tid] = True  # REAL confirmation — shares visible
                        self._settlement_created.pop(tid, None)
                        print(f'[LiveExecutor] ✅ Settlement confirmed: {tid[:16]}… ({bal:.4f} shares visible, expected {expected:.1f})')
                        # Post deferred maker TP now that shares are settled
                        if tid in self._deferred_gtc_sl:
                            _tp = self._deferred_gtc_sl.pop(tid)
                            asyncio.ensure_future(
                                self.post_maker_tp(_tp['side'], _tp['price'], _tp['qty']))
                    else:
                        age = now - self._settlement_created.get(tid, now)
                        tier = "fresh" if age < self._SETTLE_FRESH_AGE_S else "stale"
                        print(f'[LiveExecutor] ⏳ Settlement pending: {tid[:16]}… (CLOB={bal:.4f}, need {threshold:.1f}, {tier} {age:.0f}s)')
                except Exception as e:
                    logger.warning('[LiveExecutor] settlement poll error %s: %s', tid[:16], e)
            await asyncio.gather(*[_check_one(tid) for tid in check_list])
            await asyncio.sleep(self._SETTLE_FRESH_INTERVAL)
        print('[LiveExecutor] ✅ Settlement poller done — all tokens ready')

    # -- Sync helpers for thread-pool executor ---------------------------------
    def _call_update_balance_allowance(self, token_id: str):
        """Synchronous HTTP call — runs in executor, never blocks the event loop."""
        if not self._client:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            self._client.update_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=1,
            ))
        except Exception as e:
            logger.warning('[LiveExecutor] update_balance_allowance: %s', e)

    async def _async_update_balance_allowance(self, token_id: str):
        """Run sync update_balance_allowance in thread pool — proven reliable."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._settle_executor, self._call_update_balance_allowance, token_id)

    async def _async_get_balance(self, token_id: str) -> float:
        """Run sync get_balance in thread pool — proven reliable."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._settle_executor, self._call_get_balance, token_id)

    def _call_get_balance(self, token_id: str) -> float:
        """Synchronous HTTP call — runs in executor, never blocks the event loop."""
        if not self._client:
            return 0.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            result = self._client.get_balance_allowance(BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=1,
            ))
            return int(result.get('balance', 0)) / 1_000_000
        except Exception as e:
            logger.warning('[LiveExecutor] get_balance_allowance: %s', e)
            return 0.0

    def get_token_balance(self, token_id: str) -> float:
        """Return how many conditional tokens (shares) CLOB currently sees."""
        return self._call_get_balance(token_id)

    async def fetch_live_balances(self) -> dict:
        """Query CLOB for actual share balances for both sides.
        Returns {'UP': float, 'DOWN': float}.
        Calls update_balance_allowance first to ensure CLOB has the latest chain state.
        """
        loop = asyncio.get_running_loop()
        result = {'UP': 0.0, 'DOWN': 0.0}
        for side, token_id in (('UP', self._up_token_id), ('DOWN', self._down_token_id)):
            if not token_id:
                continue
            try:
                await loop.run_in_executor(self._settle_executor, self._call_update_balance_allowance, token_id)
                bal = await loop.run_in_executor(None, self._call_get_balance, token_id)
                result[side] = bal
            except Exception as _e:
                logger.warning('[LiveExecutor] fetch_live_balances %s: %s', side, _e)
        return result

    def release_exposure(self, usd_amount: float):
        """Call after a market resolves to free up the exposure budget."""
        self._open_exposure = max(0.0, self._open_exposure - usd_amount)

    async def place_passive_gtc_sell(self, side: str, price: float, qty: float):
        """Place a resting GTC SELL order and return order_id immediately.
        Does NOT poll — the GTC watcher polls status separately.
        Returns order_id string, or None on failure.
        """
        import math
        if not self.live or not self._client:
            return None
        token_id = self._up_token_id if side == 'UP' else self._down_token_id
        if not token_id:
            return None
        if qty < 0.5:
            return None
        # Snap price to tick grid (0.01) — floor so we're more likely to fill
        price = math.floor(price * 100) / 100
        price = max(0.02, min(0.98, price))
        from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
        from py_clob_client.order_builder.constants import SELL as _SELL
        loop = asyncio.get_running_loop()
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=qty,
                side=_SELL,
                fee_rate_bps=self._FEE_RATE,
            )
            opts = CreateOrderOptions(tick_size=self._TICK_SIZE, neg_risk=self._NEG_RISK)
            _exec = self._order_executor
            signed_order = await loop.run_in_executor(
                _exec, lambda: self._client.builder.create_order(order_args, opts))
            resp = await loop.run_in_executor(
                _exec, lambda: self._client.post_order(signed_order, OrderType.GTC, post_only=False))
            order_id = (resp.get('orderID') or resp.get('id') or
                        resp.get('order_id') or resp.get('orderId'))
            if order_id:
                print(f'[GTC_STOP] Placed GTC SELL {side} {qty:.1f} @ {price:.2f} id={order_id}')
                return str(order_id)
            print(f'[GTC_STOP] GTC SELL {side} no order_id in response: {str(resp)[:120]}')
            return None
        except Exception as _ge:
            _msg = str(_ge)
            if self._wallet_address:
                _msg = _msg.replace(self._wallet_address, '0x***')
            print(f'[GTC_STOP] ERROR placing GTC SELL {side} @ {price:.2f}: {_msg[:200]}')
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing CLOB order by ID. Returns True if successful."""
        if not self.live or not self._client or not order_id:
            return False
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._client.cancel(order_id))
            print(f'[GTC_STOP] Cancelled order {order_id}')
            return True
        except Exception as _ce:
            print(f'[GTC_STOP] Cancel failed {order_id}: {str(_ce)[:120]}')
            return False

    @property
    def mode(self) -> str:
        return 'LIVE' if self.live else 'PAPER'
