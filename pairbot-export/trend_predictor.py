#!/usr/bin/env python3
"""
Advanced Trend Predictor for BTC UP/DOWN Markets.

Uses real BTC spot price from Chainlink oracle (on-chain) as primary source,
with Binance/Coinbase/CoinGecko as fallbacks, to predict market outcome.
The BTC 5min UP/DOWN market resolves based on whether BTC spot price
is HIGHER (UP wins) or LOWER (DOWN wins) at close vs. open.

By tracking the actual spot price from Chainlink (the same source Polymarket
uses for resolution), we get a few-second edge over reactive market prices.
"""

import time
import math
import logging
from collections import deque
from typing import Optional, Tuple, Dict

logger = logging.getLogger(__name__)


class TrendPredictor:
    """Predicts BTC UP/DOWN market outcome using real spot price data."""

    def __init__(self):
        # === Spot price tracking ===
        self.market_open_price: Optional[float] = None   # BTC price at market open
        self.current_spot_price: Optional[float] = None   # Latest BTC spot price
        self.spot_history: deque = deque(maxlen=600)       # (timestamp, price) tuples
        self.spot_fetch_count: int = 0
        self.last_spot_fetch_time: float = 0.0

        # === Historical market outcomes ===
        self.market_history: deque = deque(maxlen=100)    # list of dicts
        self.consecutive_up: int = 0
        self.consecutive_down: int = 0
        self.total_up: int = 0
        self.total_down: int = 0

        # === Prediction state ===
        self.current_prediction: Optional[str] = None     # 'UP' or 'DOWN'
        self.prediction_confidence: float = 0.0           # 0.0 - 1.0
        self.prediction_reason: str = ''

        # === Volatility tracking ===
        self.window_high: Optional[float] = None
        self.window_low: Optional[float] = None
        self.price_changes: deque = deque(maxlen=300)     # Recent price changes

        # === Configuration ===
        self.min_delta_for_signal = 0.5     # Min $ delta to consider significant
        self.high_confidence_delta = 15.0   # $ delta for high confidence
        self.endgame_seconds = 90           # When endgame prediction kicks in
        self.critical_seconds = 30          # When prediction is near-certain

        # === Volatility regime detection ===
        self.volatility_regime: str = 'MEDIUM'  # LOW, MEDIUM, HIGH
        self.direction_flips: int = 0           # How many times direction changed
        self._prev_direction: Optional[str] = None  # Last known direction
        self._direction_history: deque = deque(maxlen=120)  # Track UP/DOWN over time
        self._flip_timestamps: list = []        # When each flip occurred

        # === EMA-based market price trend ===
        self._ema_fast: Optional[float] = None   # 5-tick EMA (fast)
        self._ema_slow: Optional[float] = None   # 15-tick EMA (slow)
        self._ema_fast_alpha: float = 2.0 / (5 + 1)   # ~0.333
        self._ema_slow_alpha: float = 2.0 / (15 + 1)  # ~0.125
        self._ema_trend: Optional[str] = None    # 'UP', 'DOWN', or None
        self._ema_crossover_strength: float = 0.0  # |fast - slow| / slow

    def reset_for_new_market(self):
        """Reset state for a new market window."""
        self.market_open_price = None
        self.current_spot_price = None
        self.spot_history.clear()
        self.price_changes.clear()
        self.window_high = None
        self.window_low = None
        self.current_prediction = None
        self.prediction_confidence = 0.0
        self.prediction_reason = ''
        # Reset volatility regime tracking
        self.volatility_regime = 'MEDIUM'
        self.direction_flips = 0
        self._prev_direction = None
        self._direction_history.clear()
        self._flip_timestamps.clear()
        # Reset EMA tracking
        self._ema_fast = None
        self._ema_slow = None
        self._ema_trend = None
        self._ema_crossover_strength = 0.0

    def set_market_open_price(self, price: float):
        """Set the BTC spot price at market open."""
        self.market_open_price = price
        self.window_high = price
        self.window_low = price
        logger.info(f"📊 Market open BTC price (reference): ${price:,.2f}")

    def update_spot_price(self, price: float, timestamp: Optional[float] = None):
        """Update with latest BTC spot price."""
        ts = timestamp or time.time()
        self.current_spot_price = price
        self.spot_history.append((ts, price))
        self.spot_fetch_count += 1
        self.last_spot_fetch_time = ts

        # Track window high/low
        if self.window_high is None or price > self.window_high:
            self.window_high = price
        if self.window_low is None or price < self.window_low:
            self.window_low = price

        # Track price changes for volatility
        if len(self.spot_history) >= 2:
            prev_price = self.spot_history[-2][1]
            self.price_changes.append(price - prev_price)

        # Auto-set open price if not set
        if self.market_open_price is None:
            self.set_market_open_price(price)

    def record_market_outcome(self, outcome: str, open_price: float, close_price: float):
        """Record a completed market outcome for future predictions."""
        delta = close_price - open_price
        self.market_history.append({
            'outcome': outcome,
            'open_price': open_price,
            'close_price': close_price,
            'delta': delta,
            'timestamp': time.time()
        })

        if outcome == 'UP':
            self.consecutive_up += 1
            self.consecutive_down = 0
            self.total_up += 1
        else:
            self.consecutive_down += 1
            self.consecutive_up = 0
            self.total_down += 1

    def get_volatility(self) -> float:
        """Estimate current BTC volatility (std dev of price changes in $)."""
        if len(self.price_changes) < 5:
            return 10.0  # Default assumption: BTC moves ~$10 per tick
        changes = list(self.price_changes)
        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        return max(0.1, math.sqrt(variance))

    def get_window_range(self) -> float:
        """Get the price range (high - low) in the current window."""
        if self.window_high is not None and self.window_low is not None:
            return self.window_high - self.window_low
        return 0.0

    def predict(self, time_to_close: Optional[float] = None) -> Tuple[Optional[str], float, str]:
        """
        Predict market outcome.

        Returns:
            (predicted_side, confidence, reason)
            predicted_side: 'UP' or 'DOWN' or None
            confidence: 0.0 - 1.0
            reason: human-readable explanation
        """
        if self.market_open_price is None or self.current_spot_price is None:
            self.current_prediction = None
            self.prediction_confidence = 0.0
            self.prediction_reason = 'No spot data'
            return None, 0.0, 'No spot data'

        delta = self.current_spot_price - self.market_open_price
        abs_delta = abs(delta)
        direction = 'UP' if delta >= 0 else 'DOWN'
        volatility = self.get_volatility()

        # === BASE CONFIDENCE from price delta ===
        # Higher delta relative to volatility = higher confidence
        if abs_delta < self.min_delta_for_signal:
            # Very small delta — essentially a coin flip
            base_conf = 0.50 + (abs_delta / self.min_delta_for_signal) * 0.05
        elif abs_delta < self.high_confidence_delta:
            # Moderate delta — growing confidence
            ratio = abs_delta / self.high_confidence_delta
            base_conf = 0.55 + ratio * 0.25  # 0.55 -> 0.80
        else:
            # Large delta — high confidence
            base_conf = min(0.95, 0.80 + (abs_delta - self.high_confidence_delta) / 50.0)

        # === TIME BOOST: Less time = more certain ===
        # With 300s left, 0% boost. With 30s left, up to 15% boost.
        # With 10s left, up to 20% boost.
        time_boost = 0.0
        if time_to_close is not None:
            if time_to_close < self.critical_seconds:
                # Critical zone: price is very unlikely to reverse
                time_boost = 0.20 * (1.0 - time_to_close / self.critical_seconds)
            elif time_to_close < self.endgame_seconds:
                # Endgame zone: increasing confidence
                time_boost = 0.10 * (1.0 - time_to_close / self.endgame_seconds)

        # === VOLATILITY ADJUSTMENT ===
        # High volatility relative to delta = less confident
        if volatility > 0 and abs_delta > 0:
            # How many standard deviations is the delta?
            z_score = abs_delta / volatility
            if z_score < 1.0:
                vol_penalty = (1.0 - z_score) * 0.10  # Up to 10% penalty
            else:
                vol_penalty = 0.0
        else:
            vol_penalty = 0.0

        # === MOMENTUM CHECK ===
        # Is price moving TOWARD or AWAY from open?
        momentum_boost = 0.0
        if len(self.spot_history) >= 10:
            recent = [p for _, p in list(self.spot_history)[-10:]]
            early = [p for _, p in list(self.spot_history)[-20:-10]] if len(self.spot_history) >= 20 else recent[:5]
            recent_avg = sum(recent) / len(recent)
            early_avg = sum(early) / len(early)
            recent_move = recent_avg - early_avg

            # If recent move is in SAME direction as delta, boost confidence
            if (delta > 0 and recent_move > 0) or (delta < 0 and recent_move < 0):
                momentum_boost = min(0.05, abs(recent_move) / max(abs_delta, 1.0) * 0.05)
            elif (delta > 0 and recent_move < 0) or (delta < 0 and recent_move > 0):
                # Moving against the delta — reduce confidence
                momentum_boost = -min(0.10, abs(recent_move) / max(abs_delta, 1.0) * 0.10)

        # === HISTORICAL PATTERN ADJUSTMENT ===
        # If we've seen 3+ consecutive outcomes in one direction, slight mean-reversion bias
        history_adj = 0.0
        if self.consecutive_up >= 3 and direction == 'UP':
            history_adj = -0.02  # Slight penalty for continuing streak
        elif self.consecutive_down >= 3 and direction == 'DOWN':
            history_adj = -0.02

        # === COMBINE ALL FACTORS ===
        confidence = base_conf + time_boost - vol_penalty + momentum_boost + history_adj
        confidence = max(0.50, min(0.98, confidence))  # Clamp to [0.50, 0.98]

        # Build reason string
        reason_parts = [
            f'BTC ${self.current_spot_price:,.1f}',
            f'Δ${delta:+,.1f}',
            f'conf={confidence:.0%}'
        ]
        if time_to_close is not None:
            reason_parts.append(f'{time_to_close:.0f}s left')
        if time_boost > 0.01:
            reason_parts.append(f'time+{time_boost:.0%}')
        if momentum_boost != 0:
            reason_parts.append(f'mom{"+" if momentum_boost > 0 else ""}{momentum_boost:.0%}')

        reason = ' | '.join(reason_parts)

        self.current_prediction = direction
        self.prediction_confidence = confidence
        self.prediction_reason = reason

        return direction, confidence, reason

    def should_endgame_position(self, time_to_close: Optional[float] = None) -> Tuple[bool, Optional[str], float]:
        """
        Check if we should aggressively position for endgame.

        Returns:
            (should_act, side, confidence)
            should_act: True if we should take action
            side: 'UP' or 'DOWN'
            confidence: prediction confidence
        """
        if time_to_close is None or time_to_close > self.endgame_seconds:
            return False, None, 0.0

        direction, confidence, _ = self.predict(time_to_close)
        if direction is None:
            return False, None, 0.0

        # Endgame thresholds:
        # 90-60s: need 70%+ confidence
        # 60-30s: need 65%+ confidence  
        # <30s:   need 60%+ confidence (almost always act)
        if time_to_close < self.critical_seconds:
            threshold = 0.60
        elif time_to_close < 60:
            threshold = 0.65
        else:
            threshold = 0.70

        should_act = confidence >= threshold
        return should_act, direction, confidence

    def get_position_sizing_multiplier(self, time_to_close: Optional[float] = None) -> float:
        """
        Get a position sizing multiplier based on prediction confidence.
        Higher confidence + less time = larger positions.

        Returns: multiplier 0.5 - 3.0
        """
        if self.prediction_confidence < 0.55:
            return 0.5  # Low confidence — small positions

        # Scale from 1.0 at 55% to 3.0 at 95% confidence
        conf_factor = (self.prediction_confidence - 0.55) / 0.40  # 0 to 1
        conf_factor = min(1.0, conf_factor)

        # Time factor: closer to end = more aggressive
        time_factor = 1.0
        if time_to_close is not None and time_to_close < self.endgame_seconds:
            time_factor = 1.0 + (1.0 - time_to_close / self.endgame_seconds) * 0.5

        multiplier = 1.0 + conf_factor * 2.0 * time_factor
        return min(3.0, multiplier)

    def update_direction_tracking(self, up_price: float, down_price: float):
        """
        Track market direction changes (flips) for whipsaw detection.
        Call this every tick with current UP/DOWN market prices.
        """
        direction = 'UP' if up_price > down_price else 'DOWN' if down_price > up_price else self._prev_direction
        if direction is None:
            return

        self._direction_history.append(direction)

        # Detect direction flip
        if self._prev_direction is not None and direction != self._prev_direction:
            self.direction_flips += 1
            self._flip_timestamps.append(time.time())
            logger.info(f"🔄 Direction FLIP #{self.direction_flips}: {self._prev_direction} → {direction}")

        self._prev_direction = direction

    def update_market_ema(self, favored_price: float):
        """
        Update EMA-based trend detection with the favored side's price.
        Uses fast (5-tick) and slow (15-tick) EMA crossover.
        """
        if self._ema_fast is None:
            self._ema_fast = favored_price
            self._ema_slow = favored_price
        else:
            self._ema_fast = self._ema_fast_alpha * favored_price + (1 - self._ema_fast_alpha) * self._ema_fast
            self._ema_slow = self._ema_slow_alpha * favored_price + (1 - self._ema_slow_alpha) * self._ema_slow

        if self._ema_slow > 0:
            self._ema_crossover_strength = abs(self._ema_fast - self._ema_slow) / self._ema_slow
            if self._ema_fast > self._ema_slow + 0.002:  # Fast above slow = bullish
                self._ema_trend = 'RISING'
            elif self._ema_fast < self._ema_slow - 0.002:  # Fast below slow = bearish
                self._ema_trend = 'FALLING'
            else:
                self._ema_trend = 'FLAT'

    def classify_volatility_regime(self, time_elapsed: Optional[float] = None) -> str:
        """
        Classify current market volatility regime.

        Uses multiple signals:
          1. Number of direction flips (most important — 50% weight)
          2. Direction history alternation pattern (25% weight)
          3. BTC spot price range in window (15% weight)
          4. Std dev of price changes (10% weight)

        Returns: 'LOW', 'MEDIUM', or 'HIGH'
        """
        score = 0.0  # Higher = more volatile

        # --- Signal 1: Direction flips (weight: 50%) ---
        # This is the STRONGEST signal. Every directional reversal is dangerous.
        # Thresholds raised to avoid false positives from noise-induced flips.
        if self.direction_flips >= 6:
            score += 0.50
        elif self.direction_flips >= 4:
            score += 0.40
        elif self.direction_flips >= 3:
            score += 0.30
        elif self.direction_flips >= 2:
            score += 0.15
        elif self.direction_flips >= 1:
            score += 0.05

        # --- Signal 2: Direction history alternation (weight: 25%) ---
        # Look at recent direction history for rapid oscillation
        if len(self._direction_history) >= 6:
            recent = list(self._direction_history)[-min(len(self._direction_history), 20):]
            changes = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1])
            change_rate = changes / len(recent)
            if change_rate >= 0.40:      # 40%+ of ticks are direction changes = very choppy
                score += 0.25
            elif change_rate >= 0.25:    # 25%+ = moderately choppy
                score += 0.15
            elif change_rate >= 0.15:
                score += 0.05

        # --- Signal 3: BTC spot price range (weight: 15%) ---
        window_range = self.get_window_range()
        if window_range > 80:
            score += 0.15
        elif window_range > 40:
            score += 0.10
        elif window_range > 20:
            score += 0.05

        # --- Signal 4: Volatility (std dev of price changes, weight: 10%) ---
        volatility = self.get_volatility()
        if volatility > 25:
            score += 0.10
        elif volatility > 15:
            score += 0.06
        elif volatility > 8:
            score += 0.02

        # Classify — lower thresholds to catch more volatile markets
        if score >= 0.40:
            self.volatility_regime = 'HIGH'
        elif score >= 0.15:
            self.volatility_regime = 'MEDIUM'
        else:
            self.volatility_regime = 'LOW'

        return self.volatility_regime

    def is_choppy_market(self) -> bool:
        """
        Detect if the market is choppy (oscillating without sustained trend).
        
        5-minute BTC markets are inherently volatile — direction flips are NORMAL.
        Only flag as choppy when there's truly no trend (EMA flat + extreme alternation).
        This flag is now advisory; it no longer blocks trading.
        """
        if self.direction_flips < 4:
            return False

        # Only choppy if EMA is flat AND alternation is extreme
        if self._ema_trend == 'FLAT' and len(self._direction_history) >= 10:
            recent = list(self._direction_history)[-min(len(self._direction_history), 20):]
            changes = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i-1])
            change_rate = changes / len(recent)
            if change_rate >= 0.40:  # 40%+ = truly oscillating with no direction
                return True

        return False

    def get_volatility_scale_factor(self) -> float:
        """
        Get a position-sizing multiplier based on volatility regime.
        HIGH vol → smaller positions, LOW vol → normal/larger positions.
        """
        if self.volatility_regime == 'HIGH':
            return 0.35  # 35% of normal size in high vol
        elif self.volatility_regime == 'MEDIUM':
            return 0.70  # 70% in medium vol
        else:
            return 1.0   # Full size in low vol

    def get_status(self) -> Dict:
        """Get current predictor status for UI/logging."""
        delta = None
        if self.market_open_price and self.current_spot_price:
            delta = self.current_spot_price - self.market_open_price

        return {
            'open_price': self.market_open_price,
            'spot_price': self.current_spot_price,
            'delta': delta,
            'prediction': self.current_prediction,
            'confidence': self.prediction_confidence,
            'reason': self.prediction_reason,
            'volatility': self.get_volatility() if len(self.price_changes) >= 5 else None,
            'window_range': self.get_window_range(),
            'history_count': len(self.market_history),
            'streak': f"UP×{self.consecutive_up}" if self.consecutive_up > 0 else f"DN×{self.consecutive_down}",
            'fetches': self.spot_fetch_count,
            'volatility_regime': self.volatility_regime,
            'direction_flips': self.direction_flips,
            'choppy': self.is_choppy_market(),
            'ema_trend': self._ema_trend,
            'vol_scale': self.get_volatility_scale_factor(),
            'reference_price': self.market_open_price,
        }


# ══════════════════════════════════════════════════════════
#  CHAINLINK ORACLE PRICE FEED
#  BTC/USD on Ethereum mainnet: 0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c
#  Uses latestRoundData() via raw JSON-RPC — no web3.py needed.
#  Chainlink updates on heartbeat (~60s) or 0.5% deviation.
# ══════════════════════════════════════════════════════════

import aiohttp as _aiohttp  # local alias for type hints

# Chainlink BTC/USD Price Feed (Ethereum mainnet, 8 decimals)
CHAINLINK_BTC_USD_ADDR = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
# keccak256("latestRoundData()")[:4]
CHAINLINK_LATEST_ROUND_DATA = "0xfeaf968c"

# Free public Ethereum RPC endpoints (tried in order)
CHAINLINK_RPC_URLS = [
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
]

# Cache: avoid hammering RPC on every 200ms tick
_chainlink_cache: dict = {'price': None, 'updated_at': 0, 'fetched_at': 0.0}
CHAINLINK_CACHE_TTL = 1.0  # re-fetch at most every 1 second


async def fetch_btc_spot_chainlink(session) -> Optional[float]:
    """
    Fetch BTC/USD from Chainlink oracle on Ethereum mainnet.

    Returns the oracle price (8 decimals). Caches for 1 second to avoid
    excessive RPC calls. Returns None on failure (callers should fallback).
    """
    import time as _t
    now = _t.time()

    # Return cached value if fresh enough
    if (_chainlink_cache['price'] is not None
            and now - _chainlink_cache['fetched_at'] < CHAINLINK_CACHE_TTL):
        return _chainlink_cache['price']

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": CHAINLINK_BTC_USD_ADDR, "data": CHAINLINK_LATEST_ROUND_DATA},
            "latest",
        ],
    }

    for rpc_url in CHAINLINK_RPC_URLS:
        try:
            async with session.post(
                rpc_url, json=payload,
                timeout=_aiohttp.ClientTimeout(total=2.5),
            ) as resp:
                result = await resp.json()
                hex_data = result.get("result", "")
                if not hex_data or hex_data == "0x":
                    continue
                raw = bytes.fromhex(hex_data[2:])
                # Slot 1 (bytes 32-63) = int256 answer
                answer = int.from_bytes(raw[32:64], 'big', signed=True)
                price = answer / 1e8
                # Slot 3 (bytes 96-127) = uint256 updatedAt
                updated_at = int.from_bytes(raw[96:128], 'big')
                staleness = now - updated_at
                # Reject if data is older than 10 minutes (stale oracle)
                if staleness > 600:
                    logger.warning(f"Chainlink BTC/USD stale ({staleness:.0f}s old), skipping")
                    continue
                # Update cache
                _chainlink_cache['price'] = price
                _chainlink_cache['updated_at'] = updated_at
                _chainlink_cache['fetched_at'] = now
                return price
        except Exception as e:
            logger.debug(f"Chainlink RPC {rpc_url} error: {e}")
            continue

    return None


async def fetch_btc_spot_binance(session) -> Optional[float]:
    """Fetch BTC spot price from Binance (free, no API key, fast)."""
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        async with session.get(url, timeout=2.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data['price'])
    except Exception as e:
        logger.debug(f"Binance fetch error: {e}")
    return None


async def fetch_btc_spot_coingecko(session) -> Optional[float]:
    """Fetch BTC spot price from CoinGecko (backup, rate-limited)."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        async with session.get(url, timeout=3.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data['bitcoin']['usd'])
    except Exception as e:
        logger.debug(f"CoinGecko fetch error: {e}")
    return None


async def fetch_btc_spot_coinbase(session) -> Optional[float]:
    """Fetch BTC spot price from Coinbase (backup)."""
    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        async with session.get(url, timeout=3.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data['data']['amount'])
    except Exception as e:
        logger.debug(f"Coinbase fetch error: {e}")
    return None


async def fetch_btc_spot(session) -> Optional[float]:
    """
    Fetch BTC spot price with fallback chain:
    1. Binance            (fastest — sub-second updates for live tracking)
    2. Chainlink oracle   (on-chain ground truth, but only updates ~60s)
    3. Coinbase           (backup)
    4. CoinGecko          (last resort)

    NOTE: Chainlink is used as PRIMARY source for the *reference* price
    (market open) via fetch_asset_price_at_timestamp(), because it matches
    the Polymarket resolution source. For *live* spot we use Binance first
    because it updates every ~200ms vs Chainlink's ~60s heartbeat.
    """
    price = await fetch_btc_spot_binance(session)
    if price:
        return price

    price = await fetch_btc_spot_chainlink(session)
    if price:
        return price

    price = await fetch_btc_spot_coinbase(session)
    if price:
        return price

    price = await fetch_btc_spot_coingecko(session)
    if price:
        return price

    return None


# ══════════════════════════════════════════════════════════
#  MULTI-ASSET SPOT PRICE FETCHING
#  Binance symbols: ETHUSDT, SOLUSDT, XRPUSDT
#  CoinGecko ids: ethereum, solana, ripple
#  Coinbase: ETH-USD, SOL-USD, XRP-USD
# ══════════════════════════════════════════════════════════

# Map asset -> Binance symbol
BINANCE_SYMBOLS = {
    'btc': 'BTCUSDT',
    'eth': 'ETHUSDT',
    'sol': 'SOLUSDT',
    'xrp': 'XRPUSDT',
}

# Map asset -> CoinGecko id
COINGECKO_IDS = {
    'btc': 'bitcoin',
    'eth': 'ethereum',
    'sol': 'solana',
    'xrp': 'ripple',
}

# Map asset -> Coinbase pair
COINBASE_PAIRS = {
    'btc': 'BTC-USD',
    'eth': 'ETH-USD',
    'sol': 'SOL-USD',
    'xrp': 'XRP-USD',
}


async def fetch_asset_spot_binance(session, asset: str) -> Optional[float]:
    """Fetch spot price for any supported asset from Binance."""
    symbol = BINANCE_SYMBOLS.get(asset.lower())
    if not symbol:
        return None
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        async with session.get(url, timeout=2.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data['price'])
    except Exception as e:
        logger.debug(f"Binance {asset} fetch error: {e}")
    return None


async def fetch_asset_spot_coinbase(session, asset: str) -> Optional[float]:
    """Fetch spot price for any supported asset from Coinbase."""
    pair = COINBASE_PAIRS.get(asset.lower())
    if not pair:
        return None
    try:
        url = f"https://api.coinbase.com/v2/prices/{pair}/spot"
        async with session.get(url, timeout=3.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data['data']['amount'])
    except Exception as e:
        logger.debug(f"Coinbase {asset} fetch error: {e}")
    return None


async def fetch_asset_spot_coingecko(session, asset: str) -> Optional[float]:
    """Fetch spot price for any supported asset from CoinGecko."""
    cg_id = COINGECKO_IDS.get(asset.lower())
    if not cg_id:
        return None
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd"
        async with session.get(url, timeout=3.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data[cg_id]['usd'])
    except Exception as e:
        logger.debug(f"CoinGecko {asset} fetch error: {e}")
    return None


async def fetch_asset_spot(session, asset: str) -> Optional[float]:
    """
    Fetch spot price for any supported asset with fallback chain.
    Supports: btc, eth, sol, xrp
    """
    # For BTC, use the optimized existing function
    if asset.lower() == 'btc':
        return await fetch_btc_spot(session)
    
    price = await fetch_asset_spot_binance(session, asset)
    if price:
        return price

    price = await fetch_asset_spot_coinbase(session, asset)
    if price:
        return price

    price = await fetch_asset_spot_coingecko(session, asset)
    if price:
        return price

    return None


async def fetch_asset_price_at_timestamp(session, asset: str, target_timestamp: float) -> Optional[float]:
    """
    Fetch asset price at a specific Unix timestamp.
    
    For BTC: tries Chainlink oracle first (on-chain ground truth),
    then falls back to Binance kline API.
    For other assets: uses Binance kline API.
    """
    # ── BTC: Try Chainlink first (matches Polymarket resolution source) ──
    if asset.lower() == 'btc':
        import time as _t
        now = _t.time()
        age = now - target_timestamp
        # If target is recent (< 120s ago), Chainlink current price is close enough
        if age < 120:
            chainlink_price = await fetch_btc_spot_chainlink(session)
            if chainlink_price:
                logger.info(f"📍 BTC reference price: ${chainlink_price:,.2f} (Chainlink oracle, {age:.0f}s ago)")
                return chainlink_price
    
    # ── Binance kline fallback (works for all assets) ──
    symbol = BINANCE_SYMBOLS.get(asset.lower())
    if not symbol:
        return None
    try:
        start_ms = int(target_timestamp * 1000)
        url = (f"https://api.binance.com/api/v3/klines"
               f"?symbol={symbol}&interval=1m&startTime={start_ms}&limit=1")
        async with session.get(url, timeout=3.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and len(data) > 0:
                    open_price = float(data[0][1])
                    logger.info(f"📍 {asset.upper()} reference price at {target_timestamp:.0f}: ${open_price:,.2f} (Binance kline)")
                    return open_price
    except Exception as e:
        logger.debug(f"Binance kline {asset} fetch error: {e}")
    
    # Fallback: current spot (Chainlink → Binance → Coinbase → CoinGecko)
    price = await fetch_asset_spot(session, asset)
    if price:
        logger.info(f"📍 {asset.upper()} fallback price: ${price:,.2f} (current spot)")
    return price


def fetch_btc_spot_sync() -> Optional[float]:
    """Synchronous version of BTC spot price fetch (for testing)."""
    import urllib.request
    import json

    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return float(data['price'])
    except Exception:
        pass

    try:
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return float(data['data']['amount'])
    except Exception:
        pass

    return None
