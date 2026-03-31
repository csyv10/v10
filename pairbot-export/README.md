# Pair Engine Strategy Package

Minimal package containing the `pair_engine` trading strategy for Polymarket.

## Files Included

- **web_bot_multi.py** - Main bot with web interface and WebSocket updates
- **pair_engine_strategy.py** - PairEngine v10 strategy (hold-to-resolution, aggressive profit-first)
- **execution_simulator.py** - Realistic order fill simulation
- **trend_predictor.py** - Trend prediction using spot price data
- **requirements.txt** - Python dependencies

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create a `.env` file with your configuration:
```bash
# Polymarket API (required for live trading)
POLYMARKET_PRIVATE_KEY=your_private_key_here
POLYMARKET_PROXY_ADDRESS=your_proxy_address_here

# Strategy configuration
STRATEGY=pair_engine
PER_MARKET_BUDGET=500
STARTING_BALANCE=2000

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## Running the Bot

### Paper Trading (Simulation Mode)
```bash
STRATEGY=pair_engine PER_MARKET_BUDGET=500 STARTING_BALANCE=2000 python3 web_bot_multi.py
```

### Live Trading
```bash
# Make sure .env contains POLYMARKET_PRIVATE_KEY and POLYMARKET_PROXY_ADDRESS
STRATEGY=pair_engine PER_MARKET_BUDGET=500 STARTING_BALANCE=2000 python3 web_bot_multi.py
```

### Background Mode (with logging)
```bash
nohup python3 web_bot_multi.py >> bot.log 2>&1 &
echo "Bot PID: $!"
```

## Web Interface

The bot runs a web server on port 8080 by default:
- Open `http://localhost:8080` to view real-time trading dashboard
- WebSocket updates show live market data and trades

## Strategy Overview

**PairEngine v10** is a hold-to-resolution strategy:
1. **Winner Entry**: Identifies and scales into the leading side (higher ask)
2. **Profit Lock**: Buys the other side when combined ask < $0.995 (guaranteed profit)
3. **Trend Flip**: Enters the other side if it rises above $0.60 (market shift)
4. **Zero Sells**: Holds all positions to market resolution

Budget: $500 per market, never exceeds allocated capital.

## Security Notes

⚠️ **CRITICAL**: Never commit `.env` or files containing private keys to git!

- Private keys should only be stored in `.env` or environment variables
- Use environment variable injection for deployment
- Rotate credentials immediately if exposed

## Support

For issues or questions, refer to the main repository documentation.
