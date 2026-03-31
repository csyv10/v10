"""
Drop-in replacement for live_executor.py using the Rust pairbot_executor.

Usage:
    # Copy pairbot_executor.so + this file to /opt/pairbot/pair_engine_package/
    # In web_bot_multi.py, change:
    #   from live_executor import LiveExecutor
    # to:
    #   from rust_live_executor import LiveExecutor
"""

import os
import sys
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional

# Add the directory containing the .so to the path
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

# Import the Rust module (pairbot_executor.so)
import pairbot_executor


@dataclass
class FillResult:
    """Matches the Python FillResult interface expected by web_bot_multi.py."""
    filled: bool = False
    desired_price: float = 0.0
    fill_price: float = 0.0
    desired_qty: float = 0.0
    filled_qty: float = 0.0
    partial: bool = False
    slippage: float = 0.0
    slippage_pct: float = 0.0
    slippage_cost: float = 0.0
    total_cost: float = 0.0
    theoretical_cost: float = 0.0
    latency_ms: float = 0.0
    book_depth_at_best: float = 0.0
    levels_consumed: int = 0
    fill_details: List[dict] = field(default_factory=list)
    reason: str = ""
    timestamp: str = ""


def _rust_to_fill(rust_result, desired_price: float, desired_qty: float) -> FillResult:
    """Convert Rust FillResult to Python FillResult."""
    fr = FillResult()
    fr.filled = rust_result.filled
    fr.fill_price = rust_result.fill_price
    fr.filled_qty = rust_result.filled_qty
    fr.total_cost = rust_result.total_cost
    fr.latency_ms = rust_result.latency_ms
    fr.reason = rust_result.reason
    fr.desired_price = desired_price
    fr.desired_qty = desired_qty
    fr.theoretical_cost = desired_price * desired_qty
    fr.timestamp = time.strftime("%H:%M:%S")

    if fr.filled and fr.fill_price > 0:
        fr.slippage = abs(fr.fill_price - desired_price)
        fr.slippage_pct = (fr.slippage / desired_price * 100) if desired_price > 0 else 0
        fr.slippage_cost = fr.slippage * fr.filled_qty
        fr.partial = fr.filled_qty < desired_qty * 0.99

    return fr


class LiveExecutor:
    """Drop-in replacement for Python LiveExecutor, backed by Rust."""

    MIN_ORDER_SIZE = 5.0

    def __init__(self, latency_ms: float = 25.0, max_slippage_pct: float = 2.0):
        self._latency_ms = latency_ms
        self._max_slippage_pct = max_slippage_pct

        # Read credentials from environment (same as Python LiveExecutor)
        api_key = os.environ.get("POLY_API_KEY", "")
        api_secret = os.environ.get("POLY_API_SECRET", "")
        api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "")
        wallet_address = os.environ.get("POLY_WALLET", "")
        private_key = os.environ.get("POLY_PRIVATE_KEY", "")
        live_trading = os.environ.get("LIVE_TRADING", "false").lower() == "true"

        self._rust = pairbot_executor.RustExecutor(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            wallet_address=wallet_address,
            private_key=private_key,
            live=live_trading,
        )

        self.live = self._rust.is_live
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rust-exec")

        # Pending TP orders: {token_id: order_id}
        self._pending_tp: dict[str, str] = {}
        # TP poll tasks
        self._tp_poll_tasks: dict[str, asyncio.Task] = {}

        # Stats tracking (minimal, for compatibility)
        self._trade_count = 0
        self._total_latency = 0.0

        mode = "LIVE (Rust)" if self.live else "PAPER (Rust)"
        print(f"[RustLiveExecutor] Initialized: {mode}")

    def set_token_ids(self, up_token_id: str, down_token_id: str):
        self._rust.set_token_ids(up_token_id, down_token_id)
        self._pending_tp.clear()

    def _get_token_id(self, side: str) -> Optional[str]:
        return self._rust.get_token_id(side)

    @property
    def mode(self) -> str:
        return self._rust.mode

    async def simulate_buy(
        self,
        side: str,
        price: float,
        qty: float,
        orderbook: dict = None,
        time_remaining_s: float = None,
    ) -> FillResult:
        """Place a maker BUY order via Rust executor."""
        token_id = self._get_token_id(side)
        if not token_id:
            return FillResult(reason="NO_TOKEN_ID")

        usd = price * qty
        if not self._rust.can_buy(usd):
            return FillResult(reason="EXPOSURE_CAP")

        if not self.live:
            # Paper mode — simulate fill
            return FillResult(
                filled=True,
                desired_price=price,
                fill_price=price,
                desired_qty=qty,
                filled_qty=qty,
                total_cost=price * qty,
                theoretical_cost=price * qty,
                reason="PAPER_BUY",
                timestamp=time.strftime("%H:%M:%S"),
            )

        loop = asyncio.get_event_loop()
        rust_result = await loop.run_in_executor(
            self._pool,
            self._rust.place_maker_buy, side, token_id, price, qty,
        )

        fr = _rust_to_fill(rust_result, price, qty)

        if fr.filled:
            self._rust.record_buy(token_id, fr.filled_qty, fr.total_cost)
            # Start settlement priming in background
            loop.run_in_executor(self._pool, self._rust.prime_settlement, token_id)
            self._trade_count += 1
            self._total_latency += fr.latency_ms

        return fr

    async def simulate_sell(
        self,
        side: str,
        price: float,
        qty: float,
        orderbook: dict = None,
        bid_price: float = None,
        stop_loss: bool = False,
    ) -> FillResult:
        """Place a SELL order via Rust executor.

        stop_loss=True  → FAK taker (immediate exit)
        stop_loss=False → maker TP (GTC post_only)
        """
        token_id = self._get_token_id(side)
        if not token_id:
            return FillResult(reason="NO_TOKEN_ID")

        if not self.live:
            return FillResult(
                filled=True,
                desired_price=price,
                fill_price=price,
                desired_qty=qty,
                filled_qty=qty,
                total_cost=price * qty,
                theoretical_cost=price * qty,
                reason="PAPER_SELL",
                timestamp=time.strftime("%H:%M:%S"),
            )

        loop = asyncio.get_event_loop()

        if stop_loss:
            # FAK taker sell — wait for settlement first
            settled_qty = await loop.run_in_executor(
                self._pool,
                self._rust.wait_for_settlement, token_id, 8.0,
            )

            sell_qty = settled_qty if settled_qty > 0.5 else qty
            sell_price = bid_price if bid_price and bid_price > 0 else price

            # Cancel any pending TP for this token first
            if token_id in self._pending_tp:
                oid = self._pending_tp.pop(token_id)
                await loop.run_in_executor(self._pool, self._rust.cancel_order, oid)

            rust_result = await loop.run_in_executor(
                self._pool,
                self._rust.place_fak_sell, side, token_id, sell_price, sell_qty,
            )

            fr = _rust_to_fill(rust_result, price, qty)

            if not fr.filled:
                # Retry with aggressive price and full CLOB balance
                balance = await loop.run_in_executor(
                    self._pool, self._rust.get_balance, token_id,
                )
                if balance > 0.5:
                    retry_price = max(0.01, sell_price - 0.02)
                    rust_result2 = await loop.run_in_executor(
                        self._pool,
                        self._rust.place_fak_sell, side, token_id, retry_price, balance,
                    )
                    fr = _rust_to_fill(rust_result2, price, qty)
                    if not fr.filled:
                        # Final retry at minimum price
                        rust_result3 = await loop.run_in_executor(
                            self._pool,
                            self._rust.place_fak_sell, side, token_id, 0.01, balance,
                        )
                        fr = _rust_to_fill(rust_result3, price, qty)

            if fr.filled:
                self._rust.record_sell(token_id, fr.filled_qty, fr.total_cost)
                self._trade_count += 1
                self._total_latency += fr.latency_ms

        else:
            # Maker TP sell — GTC post_only
            rust_result = await loop.run_in_executor(
                self._pool,
                self._rust.place_maker_tp_sell, side, token_id, price, qty,
            )

            fr = _rust_to_fill(rust_result, price, qty)

            if fr.filled:
                # Filled immediately (price was at or above bid)
                self._rust.record_sell(token_id, fr.filled_qty, fr.total_cost)
                self._trade_count += 1
                self._total_latency += fr.latency_ms
            elif fr.reason.startswith("POSTED"):
                # GTC order posted — track and poll for fill
                order_id = fr.reason.replace("POSTED id=", "")
                self._pending_tp[token_id] = order_id
                # Start background polling
                task = asyncio.ensure_future(
                    self._poll_tp_fill(side, token_id, order_id, price, qty)
                )
                self._tp_poll_tasks[token_id] = task

        return fr

    async def _poll_tp_fill(
        self, side: str, token_id: str, order_id: str, price: float, qty: float
    ):
        """Poll a GTC TP order until it fills or is cancelled."""
        loop = asyncio.get_event_loop()
        for _ in range(300):  # up to 5 minutes
            await asyncio.sleep(1.0)
            status, matched_qty, fill_price = await loop.run_in_executor(
                self._pool, self._rust.get_order_status, order_id,
            )
            if status == "matched" and matched_qty > 0:
                self._rust.record_sell(token_id, matched_qty, fill_price * matched_qty)
                self._pending_tp.pop(token_id, None)
                print(
                    f"[RustTP] {side} TP FILLED: {matched_qty:.2f} @ {fill_price:.4f}"
                )
                return
            if status in ("cancelled", "unmatched"):
                self._pending_tp.pop(token_id, None)
                return
        # Timeout — cancel
        await loop.run_in_executor(self._pool, self._rust.cancel_order, order_id)
        self._pending_tp.pop(token_id, None)

    def has_pending_tp(self, side: str) -> bool:
        token_id = self._get_token_id(side)
        return token_id in self._pending_tp if token_id else False

    def get_token_balance(self, token_id: str) -> float:
        if not self.live:
            return 0.0
        return self._rust.get_balance(token_id)

    def release_exposure(self, usd_amount: float):
        self._rust.release_exposure(usd_amount)

    def get_stats(self) -> dict:
        avg_latency = (
            self._total_latency / self._trade_count if self._trade_count > 0 else 0
        )
        return {
            "trades": self._trade_count,
            "avg_latency_ms": round(avg_latency, 1),
            "mode": self.mode,
            "engine": "rust",
        }

    def get_pnl_adjustment(self) -> float:
        return 0.0
