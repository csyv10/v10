"""Central configuration for the Polymarket Dutch Book Arbitrage Bot."""

# ── Dutch Book Strategy Parameters ──────────────────────────────────────────

# Fire when bid_up + bid_down <= 1.0 - SPREAD_THRESHOLD.
# With FOK execution the bot also verifies ask_sum is profitable before placing.
SPREAD_THRESHOLD: float = 0.04

# Total shares to deploy per cycle (split evenly when balanced, skewed when not)
ORDER_SIZE: int = 6            # base per side = ORDER_SIZE // 2 = 3

# Inventory skew factor — how aggressively to correct imbalance each cycle.
INVENTORY_SKEW: float = 0.5

# How long (ms) GTC limit orders stay open before being cancelled.
TRADE_COOLDOWN_MS: int = 5_000

# Price ladder: offsets above best bid for GTC limit orders.
# Each level is only activated if placing at that offset keeps combined cost < $1.00.
# Wider spacing improves fill rate by capturing market sweeps at multiple depths.
PRICE_LADDER: list = [0.01, 0.05, 0.10]

# Stop placing new orders this many ms before market close
STOP_BEFORE_END_MS: int = 30_000

# Minimum ask-side liquidity required before placing FOK orders.
# Prevents buying into an ask with only 1-2 shares (partial fill risk).
MIN_BID_LIQUIDITY: float = 5.0

# Price guard: only trade when BOTH sides' asks are within this range.
# Tighter range ensures near-equilibrium markets where fills are symmetric.
MIN_SIDE_PRICE: float = 0.38
MAX_SIDE_PRICE: float = 0.60

# Polymarket rejects orders below this USD value (price × shares).
MIN_ORDER_VALUE: float = 1.0

# Hard cap on position imbalance. When one side leads by this many shares,
# the bot stops buying the leading side until balance is restored.
MAX_IMBALANCE: int = 10

# Conditional hedge: after expensive side fills, monitor cheap side for reversal.
# HEDGE_MONITOR_SEC: max seconds to watch the cheap side before giving up.
# HEDGE_RISE_TRIGGER: cheap side ask must rise this many USD above its minimum.
HEDGE_MONITOR_SEC: int = 60
HEDGE_RISE_TRIGGER: float = 0.02

# Halt trading if cumulative PnL falls below -MAX_LOSS (USD)
MAX_LOSS: float = 50.0

# Maximum position size per side (shares). Bot will not buy a side beyond this.
MAX_POSITION_SIZE: int = 50

# ── API Endpoints ─────────────────────────────────────────────────────────────

CLOB_API: str = "https://clob.polymarket.com"
GAMMA_API: str = "https://gamma-api.polymarket.com"
WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Polygon mainnet chain ID
CHAIN_ID: int = 137

# ── UI Refresh ─────────────────────────────────────────────────────────────────

UI_REFRESH_HZ: float = 2.0        # How often the dashboard widgets repaint
MARKET_POLL_SEC: float = 5.0      # How often to re-check for a new market pair

# ── Orderbook Display ──────────────────────────────────────────────────────────

ORDERBOOK_DEPTH: int = 8          # Number of price levels to show per side
