"""
Polymarket real-time WebSocket price feed.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and
maintains a live best-bid / best-ask cache for subscribed token IDs.

Usage:
    feed = PolymarketWSFeed()
    await feed.start(aiohttp_session)   # call once from data_loop
    feed.subscribe(token_id)            # call whenever a new market is loaded
    bid = feed.get_bid(token_id)        # None if stale / not subscribed
    ask = feed.get_ask(token_id)
"""
import asyncio
import json
import logging
import time
from typing import Dict, Optional, Set

import aiohttp

logger = logging.getLogger(__name__)

WS_MARKET_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
_MAX_AGE_S    = 2.0   # prices older than this are considered stale


class PolymarketWSFeed:
    def __init__(self):
        self._bid:        Dict[str, float] = {}
        self._ask:        Dict[str, float] = {}
        self._book:       Dict[str, dict]  = {}   # full snapshot per token
        self._updated_at: Dict[str, float] = {}
        self._subscribed: Set[str]         = set()
        self._ws:         Optional[aiohttp.ClientWebSocketResponse] = None
        self._running     = False
        self._task:       Optional[asyncio.Task] = None
        self._session:    Optional[aiohttp.ClientSession] = None
        self.connected    = False
        self._msg_count   = 0
        self._market_events: Dict[str, list] = {}  # token_id → [asyncio.Event, …]

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe(self, token_id: str) -> None:
        """Register a token ID for subscription (sends dynamically if already connected)."""
        if not token_id or token_id in self._subscribed:
            return
        self._subscribed.add(token_id)
        if self._ws and not self._ws.closed and self._running:
            asyncio.create_task(self._send_sub([token_id]))

    def unsubscribe(self, token_id: str) -> None:
        self._subscribed.discard(token_id)
        self._bid.pop(token_id, None)
        self._ask.pop(token_id, None)
        self._book.pop(token_id, None)
        self._updated_at.pop(token_id, None)

    def get_bid(self, token_id: str, max_age_s: float = _MAX_AGE_S) -> Optional[float]:
        if time.time() - self._updated_at.get(token_id, 0) > max_age_s:
            return None
        return self._bid.get(token_id)

    def get_ask(self, token_id: str, max_age_s: float = _MAX_AGE_S) -> Optional[float]:
        if time.time() - self._updated_at.get(token_id, 0) > max_age_s:
            return None
        return self._ask.get(token_id)

    def get_book(self, token_id: str) -> Optional[dict]:
        """Return last full orderbook snapshot (for UI display)."""
        return self._book.get(token_id)

    def stats(self) -> str:
        stale = sum(
            1 for t in self._subscribed
            if time.time() - self._updated_at.get(t, 0) > _MAX_AGE_S
        )
        return (f'WS connected={self.connected} '
                f'tokens={len(self._subscribed)} stale={stale} '
                f'msgs={self._msg_count}')

    def register_event(self, token_id: str, event) -> None:
        """Register an asyncio.Event to be set on every price update for token_id."""
        if token_id not in self._market_events:
            self._market_events[token_id] = []
        if event not in self._market_events[token_id]:
            self._market_events[token_id].append(event)

    def _notify_events(self, token_id: str) -> None:
        for ev in self._market_events.get(token_id, []):
            ev.set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name='ws-price-feed')

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                logger.warning('[WSFeed] disconnected (%s) — retry in %.1fs', exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _connect(self) -> None:
        logger.info('[WSFeed] connecting to %s', WS_MARKET_URL)
        async with self._session.ws_connect(
            WS_MARKET_URL,
            heartbeat=20,          # WebSocket protocol-level PING/PONG keepalive
            max_msg_size=0,        # unlimited message size
            timeout=aiohttp.ClientWSTimeout(ws_close=5.0),
        ) as ws:
            self._ws = ws
            self.connected = True
            logger.info('[WSFeed] connected')
            print(f'[WSFeed] connected — subscribing {len(self._subscribed)} tokens')

            # Subscribe to all known tokens
            if self._subscribed:
                await self._send_sub(list(self._subscribed))

            # Application-level PING every 10s (Polymarket requirement)
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.warning('[WSFeed] WS error: %s', ws.exception())
                        break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.CLOSING):
                        break
            finally:
                ping_task.cancel()
                self._ws = None
                self.connected = False

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(10)
            if ws.closed:
                break
            try:
                await ws.send_str('PING')
            except Exception:
                break

    async def _send_sub(self, token_ids) -> None:
        if not self._ws or self._ws.closed:
            return
        msg = json.dumps({
            'type': 'market',
            'assets_ids': token_ids,
            'custom_feature_enabled': True,   # enables best_bid_ask + best_bid/ask fields
        })
        await self._ws.send_str(msg)

    # ── Message Handling ──────────────────────────────────────────────────────

    def _handle(self, raw: str) -> None:
        if raw in ('PONG', 'pong', ''):
            return
        try:
            payload = json.loads(raw)
        except Exception:
            return

        # Polymarket sends either a single event dict or a list of events
        events = payload if isinstance(payload, list) else [payload]
        now = time.time()

        for evt in events:
            if not isinstance(evt, dict):
                continue
            etype    = evt.get('event_type', '')
            token_id = evt.get('asset_id', '')
            if not token_id:
                continue

            self._msg_count += 1

            if etype == 'book':
                self._handle_book(token_id, evt, now)

            elif etype == 'price_change':
                self._handle_price_change(token_id, evt, now)

            elif etype == 'best_bid_ask':
                bb = evt.get('best_bid') or evt.get('bid')
                ba = evt.get('best_ask') or evt.get('ask')
                if bb:
                    try:
                        self._bid[token_id] = float(bb)
                    except ValueError:
                        pass
                if ba:
                    try:
                        self._ask[token_id] = float(ba)
                    except ValueError:
                        pass
                if bb or ba:
                    self._updated_at[token_id] = now
                    self._notify_events(token_id)

    def _handle_book(self, token_id: str, evt: dict, now: float) -> None:
        """Full orderbook snapshot — extract best bid/ask and store book."""
        bids = evt.get('bids') or []
        asks = evt.get('asks') or []

        best_bid = 0.0
        best_ask = float('inf')

        for b in bids:
            try:
                p = float(b['price'])
                s = float(b.get('size', 1))
                if s > 0 and p > best_bid:
                    best_bid = p
            except (KeyError, ValueError):
                pass

        for a in asks:
            try:
                p = float(a['price'])
                s = float(a.get('size', 1))
                if s > 0 and p < best_ask:
                    best_ask = p
            except (KeyError, ValueError):
                pass

        if best_bid > 0:
            self._bid[token_id] = best_bid
        if best_ask < float('inf'):
            self._ask[token_id] = best_ask
        if best_bid > 0 or best_ask < float('inf'):
            self._updated_at[token_id] = now

        # Store full book for UI display (same format as HTTP /book endpoint)
        self._book[token_id] = {'bids': bids, 'asks': asks}
        self._notify_events(token_id)

    def _handle_price_change(self, token_id: str, evt: dict, now: float) -> None:
        """Incremental update — prefer best_bid/best_ask fields (custom_feature_enabled)."""
        updated = False

        # Fast path: best_bid/best_ask provided directly
        bb = evt.get('best_bid')
        ba = evt.get('best_ask')
        if bb:
            try:
                self._bid[token_id] = float(bb)
                updated = True
            except ValueError:
                pass
        if ba:
            try:
                self._ask[token_id] = float(ba)
                updated = True
            except ValueError:
                pass

        # Apply incremental changes to the stored book and re-derive best if needed
        changes = evt.get('changes') or []
        if changes and token_id in self._book:
            book = self._book[token_id]
            bid_levels: Dict[str, float] = {
                b['price']: float(b.get('size', 0)) for b in book.get('bids', [])
            }
            ask_levels: Dict[str, float] = {
                a['price']: float(a.get('size', 0)) for a in book.get('asks', [])
            }
            for ch in changes:
                px   = ch.get('price', '')
                side = ch.get('side', '')
                sz   = float(ch.get('size', 0))
                if side == 'BUY':
                    if sz == 0:
                        bid_levels.pop(px, None)
                    else:
                        bid_levels[px] = sz
                elif side == 'SELL':
                    if sz == 0:
                        ask_levels.pop(px, None)
                    else:
                        ask_levels[px] = sz
            book['bids'] = [{'price': p, 'size': str(s)}
                            for p, s in bid_levels.items() if s > 0]
            book['asks'] = [{'price': p, 'size': str(s)}
                            for p, s in ask_levels.items() if s > 0]

            # Re-derive best bid/ask if not provided by best_bid/best_ask
            if not bb and bid_levels:
                try:
                    self._bid[token_id] = max(float(p) for p, s in bid_levels.items() if s > 0)
                    updated = True
                except ValueError:
                    pass
            if not ba and ask_levels:
                try:
                    self._ask[token_id] = min(float(p) for p, s in ask_levels.items() if s > 0)
                    updated = True
                except ValueError:
                    pass

        if updated:
            self._updated_at[token_id] = now
            self._notify_events(token_id)
