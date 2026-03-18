"""Orderbook data model and update handling."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Orderbook:
    token_id: str
    bids: List[Level] = field(default_factory=list)  # sorted desc
    asks: List[Level] = field(default_factory=list)  # sorted asc
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ── Snapshot (REST) ─────────────────────────────────────────────────────

    @classmethod
    def from_rest(cls, token_id: str, payload: dict) -> "Orderbook":
        ob = cls(token_id=token_id)
        ob._apply_snapshot(payload)
        return ob

    def _apply_snapshot(self, payload: dict) -> None:
        with self._lock:
            self.bids = sorted(
                [Level(float(b["price"]), float(b["size"])) for b in payload.get("bids", [])],
                key=lambda l: l.price,
                reverse=True,
            )
            self.asks = sorted(
                [Level(float(a["price"]), float(a["size"])) for a in payload.get("asks", [])],
                key=lambda l: l.price,
            )

    # ── Incremental WS update ────────────────────────────────────────────────

    def apply_ws_update(self, payload: dict) -> None:
        """Apply a WebSocket event.

        Polymarket sends two event types:
          - 'book'         → full snapshot (has top-level 'bids'/'asks')
          - 'price_change' → incremental delta (has 'changes' list)
        """
        if "bids" in payload or "asks" in payload:
            # Full orderbook reset — treat identically to a REST snapshot
            self._apply_snapshot(payload)
            return
        with self._lock:
            for change in payload.get("changes", []):
                side = change.get("side", "").lower()
                price = float(change["price"])
                size = float(change["size"])
                if side == "buy":
                    self._update_level(self.bids, price, size, desc=True)
                elif side == "sell":
                    self._update_level(self.asks, price, size, desc=False)

    @staticmethod
    def _update_level(levels: List[Level], price: float, size: float, desc: bool) -> None:
        for i, lvl in enumerate(levels):
            if lvl.price == price:
                if size == 0:
                    levels.pop(i)
                else:
                    lvl.size = size
                return
        if size > 0:
            levels.append(Level(price, size))
            levels.sort(key=lambda l: l.price, reverse=desc)

    # ── Convenience accessors ────────────────────────────────────────────────

    @property
    def best_ask(self) -> Optional[float]:
        with self._lock:
            return self.asks[0].price if self.asks else None

    @property
    def best_bid(self) -> Optional[float]:
        with self._lock:
            return self.bids[0].price if self.bids else None

    @property
    def best_bid_size(self) -> float:
        with self._lock:
            return self.bids[0].size if self.bids else 0.0

    @property
    def best_ask_size(self) -> float:
        with self._lock:
            return self.asks[0].size if self.asks else 0.0

    def top_asks(self, n: int) -> List[Level]:
        with self._lock:
            return list(self.asks[:n])

    def top_bids(self, n: int) -> List[Level]:
        with self._lock:
            return list(self.bids[:n])

    def spread(self) -> Optional[float]:
        a, b = self.best_ask, self.best_bid
        if a is not None and b is not None:
            return a - b
        return None
