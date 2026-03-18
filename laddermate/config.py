"""Configuration for the LadderMate ladder buy/sell strategy.

LadderMate builds positions by "climbing" the trending side of a 5-minute
binary options market. Each rung is an independent $7.50 trade with a 5-cent
spread target. If the market flips, old positions are sold at bid and a new
ladder starts on the new leading side.
"""

# ── LadderMate Strategy Parameters ─────────────────────────────────────────

# Maximum loss per market before halting. Prevents catastrophic drawdowns.
LOSS_CAP: float = 8.00

# Maximum capital deployed on one side (UP or DOWN) at any time.
MAX_SIDE_COST: float = 20.00

# Dollar size of each rung (trade). At price 0.50 this buys 15 shares.
RUNG_USD: float = 7.50

# Spread target: sell when price rises this far above entry.
RUNG_SPREAD: float = 0.050

# Price step between consecutive ladder rungs.
RUNG_STEP: float = 0.025

# Leading side price must be ≤ this to enter Scout → Ladder transition.
MAX_ENTRY_PRICE: float = 0.60

# Minimum leading side price to consider trading.
MIN_ENTRY_PRICE: float = 0.40

# ── Flip Detection ─────────────────────────────────────────────────────────

# When the opposing side's price rises above this, a potential flip is detected.
FLIP_TRIGGER: float = 0.45

# Number of consecutive ticks the opposing side must stay above FLIP_TRIGGER
# before confirming a flip. Prevents false positives from noise.
FLIP_CONFIRM_TICKS: int = 6

# ── Exit Window ────────────────────────────────────────────────────────────

# Seconds before market end to suppress flip detection and let positions resolve.
# In the last EXIT_TTC seconds, flips are often noise — better to hold and settle.
EXIT_TTC: float = 50.0

# Stop placing NEW orders this many seconds before market close.
STOP_BEFORE_END_SEC: float = 30.0

# ── CLOB Settlement Polling ───────────────────────────────────────────────

# How often (seconds) to poll the CLOB API to check if bought shares have
# settled and are available for selling. Instead of waiting a fixed 22s,
# the engine actively checks the fills/trades API every SETTLE_POLL_SEC
# to confirm settlement — selling as soon as shares are confirmed on-chain.
SETTLE_POLL_SEC: float = 1.0

# ── Timing ────────────────────────────────────────────────────────────────

# Seconds to wait after a new market is detected before trading.
# Avoids early volatility at market open.
MARKET_COOLDOWN_SEC: float = 15.0

# How often (seconds) the strategy loop checks for new signals.
EVAL_INTERVAL_SEC: float = 1.0

# How often (seconds) to poll for sell opportunities on open rungs.
SELL_CHECK_INTERVAL_SEC: float = 0.5
