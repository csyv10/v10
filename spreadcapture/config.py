"""Configuration for the Spread Capture Market Making Bot."""

# ── Strategy Parameters ────────────────────────────────────────────────────

# Minimum spread to capture: combined bids must be <= 1.0 - TARGET_SPREAD
TARGET_SPREAD: float = 0.03

# Shares placed per side per quote cycle
ORDER_SIZE: int = 10

# Maximum open inventory per side (won't bid more once reached)
MAX_INVENTORY: int = 50

# How often to cancel and re-quote (milliseconds)
REFRESH_INTERVAL_MS: int = 2_000

# Minimum guaranteed edge above break-even:
# 1.0 - bid_up - bid_down must be >= EDGE_THRESHOLD
EDGE_THRESHOLD: float = 0.01

# Inventory skew factor: 0 = no adjustment, 1 = aggressive rebalancing
INVENTORY_SKEW: float = 0.5

# Only quote when prices are within this range
MIN_PRICE: float = 0.0
MAX_PRICE: float = 1.0

# Stop quoting this many ms before the market window closes
STOP_BEFORE_END_MS: int = 30_000

# Halt all trading if cumulative PnL drops below -MAX_LOSS USD
MAX_LOSS: float = 50.0

# ── UI ─────────────────────────────────────────────────────────────────────
UI_REFRESH_HZ: float = 2.0
ORDERBOOK_DEPTH: int = 8
