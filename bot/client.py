"""Polymarket API client wrapper.

Combines:
  - CLOB REST API (authenticated, via py-clob-client)
  - Gamma Markets REST API (public, for market discovery)
  - CLOB WebSocket (public, for live orderbook streaming)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from typing import Any, Callable, Dict, List, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from config import CLOB_API, GAMMA_API, WS_URL, CHAIN_ID
from bot.orderbook import Orderbook

log = logging.getLogger(__name__)


def _parse_market_fields(m: Dict) -> Dict:
    """Gamma API returns outcomes/clobTokenIds as JSON strings — parse them."""
    m = dict(m)
    for field in ("outcomes", "clobTokenIds", "outcomePrices"):
        val = m.get(field)
        if isinstance(val, str):
            try:
                m[field] = json.loads(val)
            except (ValueError, TypeError):
                pass
    return m


class PolymarketClient:
    """Thin wrapper around the Polymarket APIs."""

    def __init__(self, paper: bool = False) -> None:
        self.paper = paper
        self._http = httpx.AsyncClient(timeout=15.0)
        self._clob_client: Optional[Any] = None  # py-clob-client ClobClient

    # ── Authenticated CLOB client ─────────────────────────────────────────────

    def init_clob_client(self) -> None:
        """Lazily initialise the py-clob-client (requires private key in env)."""
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        pk = os.environ.get("PRIVATE_KEY")
        if not pk:
            raise RuntimeError("PRIVATE_KEY env variable not set")

        api_key = os.environ.get("CLOB_API_KEY")
        secret = os.environ.get("CLOB_SECRET")
        passphrase = os.environ.get("CLOB_PASSPHRASE")

        # If WALLET_ADDRESS is set it means the user has a Polymarket proxy wallet.
        # The private key belongs to the EOA that controls the proxy; we must pass
        # funder=proxy_address and signature_type=1 (POLY_PROXY) so the CLOB client signs correctly.
        # Note: signature_type=1 = POLY_PROXY, signature_type=2 = POLY_GNOSIS_SAFE (different!)
        funder = os.environ.get("WALLET_ADDRESS", "").strip() or None
        sig_type = 1 if funder else 0

        if api_key and secret and passphrase:
            creds = ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase)
            self._clob_client = ClobClient(
                host=CLOB_API,
                key=pk,
                chain_id=CHAIN_ID,
                creds=creds,
                signature_type=sig_type,
                funder=funder,
            )
            log.info("CLOB client initialised with L2 API creds (sig_type=%d funder=%s)",
                     sig_type, funder or "EOA")
        else:
            self._clob_client = ClobClient(
                host=CLOB_API,
                key=pk,
                chain_id=CHAIN_ID,
                signature_type=sig_type,
                funder=funder,
            )
            log.info("CLOB client initialised with L1 auth (sig_type=%d funder=%s)",
                     sig_type, funder or "EOA")

    @property
    def clob(self) -> Any:
        if self._clob_client is None:
            self.init_clob_client()
        return self._clob_client

    # ── Market discovery (Gamma API) ──────────────────────────────────────────

    async def get_5min_markets(self, underlying: str = "BTC") -> List[Dict]:
        """Find active 5-min UP/DOWN markets via timestamp-based slug probing.

        Polymarket slug format: {underlying}-updown-5m-{start_unix_ts}
        where start_unix_ts is the UTC start of the 5-minute window (multiple of 300).

        Standard Gamma API list endpoints do not return these restricted markets,
        so we probe the expected slugs for the current and upcoming windows.
        """
        target = underlying.lower()
        now = int(_time.time())
        current_period = (now // 300) * 300  # start of current 5-min window

        five_min: List[Dict] = []
        seen: set = set()

        # Check the current window (-1) and next few windows to have data ready
        for offset in range(-1, 4):
            start_ts = current_period + offset * 300
            slug = f"{target}-updown-5m-{start_ts}"
            try:
                resp = await self._http.get(f"{GAMMA_API}/events", params={"slug": slug})
                if resp.status_code != 200:
                    continue
                events = resp.json()
                for event in events:
                    for m in event.get("markets", []):
                        m = _parse_market_fields(m)
                        mid = m.get("id", slug)
                        if mid not in seen:
                            seen.add(mid)
                            five_min.append(m)
            except Exception as exc:
                log.debug("Slug probe %s: %s", slug, exc)

        return five_min

    async def get_market_outcome(self, slug: str) -> Optional[str]:
        """Query Gamma API for a resolved market's outcome. Returns 'UP', 'DOWN', or None."""
        try:
            resp = await self._http.get(f"{GAMMA_API}/events", params={"slug": slug})
            if resp.status_code != 200:
                return None
            for event in resp.json():
                for m in event.get("markets", []):
                    m = _parse_market_fields(m)
                    prices = m.get("outcomePrices", [])
                    outcomes = m.get("outcomes", [])
                    for i, p in enumerate(prices):
                        try:
                            if float(p) > 0.9 and i < len(outcomes):
                                o = str(outcomes[i]).lower()
                                if o in ("up", "higher"):
                                    return "UP"
                                elif o in ("down", "lower"):
                                    return "DOWN"
                        except (ValueError, TypeError):
                            pass
        except Exception as exc:
            log.debug("get_market_outcome %s: %s", slug, exc)
        return None

    async def find_up_down_pair(self, underlying: str = "BTC") -> Optional[Dict[str, Dict]]:
        """Return {up: market_dict, down: market_dict} for the current 5-min pair.

        Picks the market whose end date is furthest in the future while not yet closed.
        Token IDs come from clobTokenIds, indexed to match the outcomes array.
        """
        markets = await self.get_5min_markets(underlying)

        from datetime import datetime, timezone as _tz
        now_iso = datetime.now(tz=_tz.utc).isoformat()

        best: Optional[Dict[str, Dict]] = None
        earliest_end: str = "9999"  # pick the soonest-expiring future market

        for m in markets:
            if m.get("closed", False):
                continue

            outcomes: List[str] = m.get("outcomes", [])
            token_ids: List[str] = m.get("clobTokenIds", [])

            if len(outcomes) < 2 or len(token_ids) < 2:
                continue

            up_idx: Optional[int] = None
            down_idx: Optional[int] = None
            for i, outcome in enumerate(outcomes):
                o = outcome.lower()
                if o in ("up", "higher"):
                    up_idx = i
                elif o in ("down", "lower"):
                    down_idx = i

            if up_idx is None or down_idx is None:
                continue

            end_str = m.get("endDate") or m.get("endDateIso", "")
            if not end_str:
                continue
            end_norm = end_str.replace("Z", "+00:00")

            # Skip already-expired markets (API may lag closing them)
            if end_norm <= now_iso:
                continue

            # Pick the market whose window ends soonest — that is the active one
            if end_norm < earliest_end:
                earliest_end = end_norm
                up_market = dict(m)
                up_market["_token_id"] = token_ids[up_idx]
                down_market = dict(m)
                down_market["_token_id"] = token_ids[down_idx]
                best = {"up": up_market, "down": down_market}

        return best

    # ── Orderbook (REST snapshot) ─────────────────────────────────────────────

    async def get_orderbook(self, token_id: str) -> Orderbook:
        resp = await self._http.get(f"{CLOB_API}/book", params={"token_id": token_id})
        resp.raise_for_status()
        return Orderbook.from_rest(token_id, resp.json())

    # ── Order placement ────────────────────────────────────────────────────────

    def place_limit_gtc(
        self, token_id: str, price: float, size: int, side: str = "BUY"
    ) -> tuple:
        """Place a Good-Till-Cancelled limit order.

        Returns (order_id: str, filled: int).
        In paper mode the fill is simulated immediately (100%).
        In live mode, only shares immediately matched are counted — the rest
        remain open in the book until cancel_order() is called.
        """
        if self.paper:
            import uuid
            fake_id = f"paper-{uuid.uuid4().hex[:16]}"
            # Return 0 immediate fills — strategy will simulate fills from live orderbook
            log.info("[PAPER] GTC BUY %d @ %.4f token=%s → queued", size, price, token_id[:12])
            return fake_id, 0

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob_side = BUY if side.upper() == "BUY" else SELL
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=clob_side)

        # Retry up to 3 times on transient network errors (tick_size/neg_risk cache
        # miss or temporary CLOB API timeout — usually resolves within 1-2 retries).
        import time as _t
        from py_clob_client.exceptions import PolyApiException as _PolyEx
        last_exc = None
        for attempt in range(3):
            try:
                signed_order = self.clob.create_order(order_args)
                resp = self.clob.post_order(signed_order, OrderType.GTC)
                last_exc = None
                break
            except _PolyEx as exc:
                if "Request exception" in str(exc):
                    last_exc = exc
                    if attempt < 2:
                        log.warning("place_limit_gtc transient error (attempt %d/3): %s — retrying", attempt + 1, exc)
                        _t.sleep(1)
                    continue
                raise  # non-transient error — propagate immediately
        if last_exc is not None:
            raise last_exc

        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        status   = resp.get("status", "")  if isinstance(resp, dict) else str(resp)

        # Immediate fill (order crossed the book entirely)
        if status in ("matched", "MATCHED"):
            filled = size
        else:
            # Partial immediate fill may be in sizeFilled / matchedComps
            filled_raw = resp.get("sizeFilled", 0) if isinstance(resp, dict) else 0
            try:
                filled = int(float(filled_raw))
            except (TypeError, ValueError):
                filled = 0

        log.info("GTC BUY %d @ %.4f → id=%s status=%s filled=%d", size, price, order_id[:16], status, filled)
        return order_id, filled

    def place_market_fok(
        self, token_id: str, price: float, size: int, side: str = "BUY"
    ) -> tuple:
        """Place a Fill-Or-Kill taker order that crosses the spread immediately.

        price should be set at or above the current best ask so the order
        matches resting sell orders and fills instantly. Any unfilled remainder
        is cancelled (FOK semantics — no resting bid left on the book).

        Returns (order_id: str, filled: int).
        """
        if self.paper:
            import uuid
            fake_id = f"paper-fok-{uuid.uuid4().hex[:12]}"
            log.info("[PAPER] FOK BUY %d @ %.4f token=%s → simulated full fill", size, price, token_id[:12])
            return fake_id, size

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        clob_side = BUY if side.upper() == "BUY" else SELL
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=clob_side)

        try:
            signed_order = self.clob.create_order(order_args)
            resp = self.clob.post_order(signed_order, OrderType.FOK)
        except Exception:
            raise

        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        status   = resp.get("status", "")  if isinstance(resp, dict) else str(resp)

        if status in ("matched", "MATCHED"):
            filled = size
        else:
            filled_raw = resp.get("sizeFilled", 0) if isinstance(resp, dict) else 0
            try:
                filled = int(float(filled_raw))
            except (TypeError, ValueError):
                filled = 0

        log.info("FOK BUY %d @ %.4f → id=%s status=%s filled=%d", size, price, order_id[:16], status, filled)
        return order_id, filled

    def get_order_filled(self, order_id: str) -> Optional[int]:
        """Return the number of shares actually filled for an order.

        Called after cancel_order() to get the real (possibly partial) fill count.
        Returns None if the API query fails (so callers can distinguish error from 0 fills).
        Returns 0 if the query succeeded but nothing was filled.
        """
        if self.paper or not order_id:
            return 0
        try:
            order = self.clob.get_order(order_id)
            if not isinstance(order, dict):
                return None
            # py-clob-client uses 'size_matched' or 'sizeFilled' depending on version
            raw = (
                order.get("size_matched")
                or order.get("sizeFilled")
                or order.get("matched_amount")
                or 0
            )
            return int(float(raw))
        except Exception as exc:
            log.warning("get_order_filled %s failed: %s", order_id[:16], exc)
            return None

    def get_fills_for_market(self, token_ids: list, after_ts: float = 0.0) -> list:
        """Return all filled trades for the given token IDs from the last 10 minutes.

        Used to initialise position on bot startup in live mode.
        Returns list of dicts with keys: asset_id, price, size.

        Trade structure differs by role:
          TAKER (order crossed the book immediately): our asset is the top-level asset_id.
          MAKER (passive limit order filled): our fill is inside maker_orders where
            maker_address matches our proxy wallet.
        """
        if self.paper:
            return []

        import time as _time
        from py_clob_client.clob_types import TradeParams

        token_id_set = set(token_ids)
        our_address = os.environ.get("WALLET_ADDRESS", "").strip().lower()
        results = []

        try:
            if after_ts <= 0:
                after_ts = _time.time() - 600  # default: last 10 minutes
            trades = self.clob.get_trades(TradeParams(after=int(after_ts)))

            for trade in trades:
                trader_side = trade.get("trader_side", "")

                if trader_side == "TAKER":
                    # We crossed the book — top-level asset_id is what we bought
                    asset_id = trade.get("asset_id", "")
                    if asset_id in token_id_set:
                        try:
                            results.append({
                                "asset_id": asset_id,
                                "size": float(trade.get("size", 0)),
                                "price": float(trade.get("price", 0)),
                            })
                        except (ValueError, TypeError):
                            pass

                elif trader_side == "MAKER":
                    # Our fills are inside maker_orders — filter by our wallet address
                    for mo in trade.get("maker_orders", []):
                        if mo.get("maker_address", "").lower() != our_address:
                            continue
                        asset_id = mo.get("asset_id", "")
                        if asset_id in token_id_set:
                            try:
                                results.append({
                                    "asset_id": asset_id,
                                    "size": float(mo.get("matched_amount", 0)),
                                    "price": float(mo.get("price", 0)),
                                })
                            except (ValueError, TypeError):
                                pass

        except Exception as exc:
            log.warning("get_fills_for_market failed: %s", exc)

        return results

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single open order. Returns True on success."""
        if self.paper:
            return True  # nothing to cancel in paper mode
        if not order_id:
            return False
        try:
            self.clob.cancel(order_id)
            return True
        except Exception as exc:
            log.warning("cancel_order %s failed: %s", order_id[:16], exc)
            return False

    async def cancel_open_orders(self) -> int:
        """Cancel all open orders. Returns count cancelled. Used on market start."""
        if self.paper:
            return 0
        loop = asyncio.get_event_loop()
        try:
            orders = await loop.run_in_executor(None, self.clob.get_orders)
            count = 0
            for o in (orders or []):
                oid = o.get("id") or o.get("orderID", "")
                if oid and self.cancel_order(oid):
                    count += 1
            log.info("Cancelled %d open orders on market start", count)
            return count
        except Exception as exc:
            log.warning("cancel_open_orders failed: %s", exc)
            return 0

    # ── Balance queries ──────────────────────────────────────────────────────

    def get_conditional_balance(self, token_id: str) -> int:
        """Return the on-chain CTF token balance for a specific token_id.

        Uses the CLOB get_balance_allowance endpoint with AssetType.CONDITIONAL
        to check the actual number of conditional tokens held in the wallet.
        This reflects settled (on-chain) shares, not pending trades.

        Returns the number of shares (int), or 0 on error / paper mode.
        """
        if self.paper:
            return 0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
            )
            resp = self.clob.get_balance_allowance(params)
            if isinstance(resp, dict):
                raw = resp.get("balance", "0")
                # Balance is in wei (1e6 decimals for USDC-backed CTF tokens)
                return int(float(raw) / 1e6)
            return 0
        except Exception as exc:
            log.debug("get_conditional_balance %s: %s", token_id[:16], exc)
            return 0

    # ── WebSocket stream ──────────────────────────────────────────────────────

    async def stream_orderbooks(
        self,
        token_ids: List[str],
        on_update: Callable[[str, dict], None],
        stop_event: asyncio.Event,
    ) -> None:
        """Open a WebSocket and call on_update(token_id, payload) on every book event.

        Automatically reconnects on disconnect until stop_event is set.
        """
        subscribe_msg = json.dumps({
            "auth": {},
            "type": "market",
            "assets_ids": token_ids,
        })

        while not stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    await ws.send(subscribe_msg)
                    log.info("WebSocket connected, subscribed to %d tokens", len(token_ids))
                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                            # Polymarket may send a list of events or a single dict
                            items = msg if isinstance(msg, list) else [msg]
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                event_type = item.get("event_type", "")
                                asset_id = item.get("asset_id", "")
                                if event_type in ("book", "price_change") and asset_id:
                                    on_update(asset_id, item)
                        except (json.JSONDecodeError, KeyError):
                            pass
            except (ConnectionClosed, OSError) as exc:
                if stop_event.is_set():
                    break
                log.warning("WebSocket disconnected (%s), reconnecting in 3s…", exc)
                await asyncio.sleep(3)

    async def close(self) -> None:
        await self._http.aclose()
