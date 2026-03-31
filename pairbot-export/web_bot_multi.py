#!/usr/bin/env python3
"""
Polymarket Multi-Market Bot - BTC 5m Up/Down Tracker
Web-based interface with real-time updates via WebSocket.
NEW: Dynamic Delta Neutral Arbitrage Strategy - Mean Reversion
"""

import asyncio
import aiohttp
import json
import time
import hashlib as _hashlib
import secrets as _secrets
import hmac as _hmac_mod
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
from aiohttp import web
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Import strategy — switch between ArbitrageStrategy and MarketMakerStrategy via env var
# Set STRATEGY=market_maker to use the new market-making strategy
# Strategy selection via env var STRATEGY=
#   pure_arb   — Pure Arbitrage (buy cheap side, lock when pair < 0.95, grow)
#   market_maker — Spread-capture market maker
#   scalper      — Adaptive micro-scalper
#   arbitrage    — Original directional arbitrage
#   pair_engine  — PairEngine v13 conviction entry + dynamic hedge
#   gabagaba     — GabaGaba: cheap entry <= 0.46, hunt arb lock, aggressive grow
#   laddermate   — LadderMate: ladder buy/sell on trending side, flip recovery
#   opportunist  — OppShot: buy both sides cheap at different times, lock pairs
_strategy_choice = os.getenv('STRATEGY', 'pair_engine').lower()
if _strategy_choice == 'pure_arb':
    from pure_arb_strategy import PureArbitrageStrategy as ArbitrageStrategy
    print("📊 Using PureArbitrageStrategy (buy-lock-grow arbitrage)")
elif _strategy_choice == 'market_maker':
    from market_maker_strategy import MarketMakerStrategy as ArbitrageStrategy
    print("📊 Using MarketMakerStrategy (spread capture)")
elif _strategy_choice == 'scalper':
    from adaptive_scalper_strategy import AdaptiveScalperStrategy as ArbitrageStrategy
elif _strategy_choice == 'hybrid_scalper':
    from hybrid_scalper_strategy import HybridScalperStrategy as ArbitrageStrategy
    print("📊 Using HybridScalperStrategy (momentum+deep-value scalper, buy & sell)")
    print("📊 Using AdaptiveScalperStrategy (micro scalper)")
elif _strategy_choice == 'pair_engine':
    from pair_engine_strategy import PairEngineStrategy as ArbitrageStrategy
    print("📊 Using PairEngineStrategy v7 (hold-to-resolution, zero sells)")
elif _strategy_choice == 'gabagaba':
    from gabagaba_strategy import GabaGabaStrategy as ArbitrageStrategy
    print("📊 Using GabaGabaStrategy (cheap-entry -> arb-lock -> aggressive grow)")
elif _strategy_choice == 'gaba':
    from gaba_strategy import GabaStrategy as ArbitrageStrategy
    print("📊 Using GabaStrategy (dutch book: buy both sides cheap, hold to resolution)")
elif _strategy_choice == 'laddermate':
    from laddermate_strategy import LadderMateStrategy as ArbitrageStrategy
    print("📊 Using LadderMateStrategy (ladder buy/sell + flip recovery)")
elif _strategy_choice == 'opportunist':
    from opportunist_strategy import OppShotStrategy as ArbitrageStrategy
    print("📊 Using OppShotStrategy (buy both sides cheap, lock matched pairs)")
else:
    from arbitrage_strategy import ArbitrageStrategy
    print("📊 Using ArbitrageStrategy (directional arb)")
from execution_simulator import ExecutionSimulator
try:
    from live_executor import LiveExecutor
except ImportError:
    LiveExecutor = None
from trend_predictor import (
    fetch_btc_spot,
    fetch_asset_spot, fetch_asset_price_at_timestamp,
)

# Supported assets — 5-minute up/down markets
# Override with ASSETS=btc  or  ASSETS=btc,eth  in .env to restrict tracking
_ALL_ASSETS = ['btc', 'eth', 'sol', 'xrp']
_asset_env = os.getenv('ASSETS', os.getenv('ASSET', '')).strip().lower()
if _asset_env:
    SUPPORTED_ASSETS = [a.strip() for a in _asset_env.split(',') if a.strip() in _ALL_ASSETS]
    if not SUPPORTED_ASSETS:
        print(f'[WARNING] ASSET(S)={_asset_env!r} not recognised — defaulting to all assets')
        SUPPORTED_ASSETS = list(_ALL_ASSETS)
    else:
        print(f'[Config] Tracking assets: {SUPPORTED_ASSETS}')
else:
    SUPPORTED_ASSETS = list(_ALL_ASSETS)

# ── Auth / Session management ─────────────────────────────────────────────────
_sessions: dict = {}  # {token: expiry_timestamp}
_SESSION_LIFETIME = 30 * 24 * 3600  # 30 days

def _pw_hash(password: str, salt: str) -> str:
    return _hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260_000).hex()

def _verify_dashboard_password(password: str) -> bool:
    stored_hash = os.getenv('DASHBOARD_PASSWORD_HASH', '').strip()
    stored_salt = os.getenv('DASHBOARD_PASSWORD_SALT', '').strip()
    if not stored_hash:
        return True  # No password set = open access
    return _hmac_mod.compare_digest(_pw_hash(password, stored_salt), stored_hash)

def _create_session() -> str:
    token = _secrets.token_hex(32)
    now = time.time()
    _sessions[token] = now + _SESSION_LIFETIME
    # Prune expired sessions
    expired = [t for t, exp in list(_sessions.items()) if exp < now]
    for t in expired:
        _sessions.pop(t, None)
    return token

def _is_session_valid(token: str) -> bool:
    if not token:
        return False
    return time.time() < _sessions.get(token, 0)

def _password_required() -> bool:
    return bool(os.getenv('DASHBOARD_PASSWORD_HASH', '').strip())

# Per-asset market window configuration
ASSET_MARKET_CONFIG = {
    'btc': {'window_seconds': 300,  'suffix': '5m'},
    'eth': {'window_seconds': 300,  'suffix': '5m'},
    'sol': {'window_seconds': 300,  'suffix': '5m'},
    'xrp': {'window_seconds': 300,  'suffix': '5m'},
}

# Legacy globals (used as defaults if asset not in config)
MARKET_WINDOW_SECONDS = 300
MARKET_WINDOW_SUFFIX = "5m"
URGENCY_THRESHOLD_SECONDS = 90

# Per-asset budget — $500 per market
ASSET_BUDGETS = {
    'btc': 500.0,
    'eth': 500.0,
    'sol': 500.0,
    'xrp': 500.0,
}

# Manual markets to track (leave empty for auto-discovery)
MANUAL_MARKETS = []

# Login page template — use % formatting to avoid conflict with CSS braces
_LOGIN_HTML_BASE = (
    '<!DOCTYPE html><html lang="en"><head>'
    '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">'
    '<title>PairBot Login</title><style>'
    '*{margin:0;padding:0;box-sizing:border-box}'
    'body{background:#0c0c0c;color:#fff;font-family:Consolas,Monaco,monospace;'
    'display:flex;align-items:center;justify-content:center;min-height:100vh;}'
    '.card{background:#1a1a2e;border:1px solid #3b82f6;border-radius:12px;'
    'padding:40px 36px;width:100%;max-width:360px;}'
    'h1{color:#3b82f6;font-size:20px;margin-bottom:6px;text-align:center;}'
    '.sub{color:#888;font-size:12px;text-align:center;margin-bottom:28px;}'
    'label{display:block;font-size:11px;color:#9ca3af;margin-bottom:5px;}'
    'input[type=password]{width:100%;background:#0c0c0c;border:1px solid #333;'
    'border-radius:4px;color:#fff;padding:10px 12px;font-family:inherit;font-size:14px;margin-bottom:18px;}'
    'input[type=password]:focus{border-color:#3b82f6;outline:none;}'
    'button{width:100%;padding:11px;background:#3b82f6;border:none;border-radius:4px;'
    'color:#fff;font-weight:bold;font-size:14px;cursor:pointer;font-family:inherit;}'
    'button:hover{background:#2563eb;}'
    '.err{color:#ef4444;font-size:12px;text-align:center;margin-top:12px;}'
    '</style></head><body>'
    '<div class="card"><h1>PairBot</h1>'
    '<div class="sub">Polymarket Trading Bot</div>'
    '<form method="post" action="/login">'
    '<label>Password</label>'
    '<input type="password" name="password" autofocus autocomplete="current-password">'
    '<button type="submit">Sign in</button>'
    '__ERROR__'
    '</form></div></body></html>'
)
def _make_login_html(error: str = '') -> str:
    return _LOGIN_HTML_BASE.replace('__ERROR__', error)

# HTML Template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>PairBot Workspace</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0f;--surface:#12131a;--surface2:#1a1b26;--border:#2a2b3d;
  --text:#e1e2e8;--muted:#6b6d80;--dim:#3d3f52;
  --green:#00d68f;--red:#ff4d6a;--yellow:#ffc145;--blue:#4d8eff;--purple:#a78bfa;
  --cyan:#22d3ee;
  --card-radius:12px;--node-shadow:0 4px 24px rgba(0,0,0,.4);
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px}

/* ── Top bar ── */
#topbar{position:fixed;top:0;left:0;right:0;z-index:200;height:48px;display:flex;align-items:center;padding:0 16px;background:var(--surface);border-bottom:1px solid var(--border);gap:12px}
#topbar .logo{font-weight:700;font-size:15px;color:var(--blue);letter-spacing:.5px;display:flex;align-items:center;gap:8px}
#topbar .logo svg{width:20px;height:20px}
.spacer{flex:1}
.top-stat{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.top-stat .tv{font-weight:600;color:var(--text);font-size:13px}
.top-stat .tv.pos{color:var(--green)}.top-stat .tv.neg{color:var(--red)}

/* Paper/Live toggle */
.mode-switch{display:flex;background:var(--bg);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.mode-switch button{border:none;background:none;color:var(--muted);font:inherit;font-size:12px;font-weight:600;padding:6px 16px;cursor:pointer;transition:all .15s}
.mode-switch button.active{background:var(--blue);color:#fff}
.mode-switch button:first-child.active{background:var(--green);color:#0a0a0f}
.mode-switch button:last-child.active{background:var(--red);color:#fff}

.top-btn{background:none;border:1px solid var(--border);border-radius:8px;color:var(--muted);font:inherit;font-size:12px;padding:6px 14px;cursor:pointer;transition:all .15s;display:flex;align-items:center;gap:5px}
.top-btn:hover{border-color:var(--text);color:var(--text)}
.top-btn svg{width:14px;height:14px}

/* Connection indicator */
.conn-dot{width:8px;height:8px;border-radius:50%;background:var(--red)}
.conn-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)}

/* ── Workspace canvas ── */
#workspace{position:fixed;top:48px;left:0;right:0;bottom:0;overflow:hidden;cursor:grab;background:
  radial-gradient(circle at 50% 50%, rgba(77,142,255,.03) 0%, transparent 70%)}
#workspace:active{cursor:grabbing}
#workspace.connecting{cursor:crosshair}

/* Grid dots */
#grid-canvas{position:absolute;inset:0;pointer-events:none}

/* Transformable scene layer */
#scene{position:absolute;inset:0;transform-origin:0 0;pointer-events:none}
#scene>*{pointer-events:auto}

/* SVG layer for connections */
#connections-svg{position:absolute;left:0;top:0;width:10000px;height:10000px;pointer-events:none;z-index:5}
#connections-svg path{fill:none;stroke:var(--green);stroke-width:2;stroke-linecap:round;filter:drop-shadow(0 0 4px rgba(0,214,143,.3))}
#connections-svg path.temp{stroke-dasharray:6 4;opacity:.6}

/* Zoom indicator */
#zoom-indicator{position:fixed;bottom:12px;left:12px;z-index:200;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:4px 10px;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:6px;user-select:none}
#zoom-indicator button{background:none;border:1px solid var(--border);border-radius:4px;color:var(--muted);width:24px;height:24px;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .1s}
#zoom-indicator button:hover{color:var(--text);border-color:var(--text)}

/* ── Node cards ── */
.node{position:absolute;z-index:10;min-width:220px;background:var(--surface);border:1px solid var(--border);border-radius:var(--card-radius);box-shadow:var(--node-shadow);user-select:none;transition:box-shadow .15s}
.node:hover{box-shadow:0 6px 32px rgba(0,0,0,.5)}
.node.selected{border-color:var(--blue);box-shadow:0 0 0 2px rgba(77,142,255,.25),var(--node-shadow)}
.node-header{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border);cursor:grab;border-radius:var(--card-radius) var(--card-radius) 0 0}
.node-header:active{cursor:grabbing}
.node-icon{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
.node-title{font-weight:600;font-size:13px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.node-badge{font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;text-transform:uppercase;letter-spacing:.5px}
.node-body{padding:12px 14px}

/* Bot node */
.bot-node .node-icon{background:rgba(167,139,250,.15);color:var(--purple)}
.bot-node .node-header{background:linear-gradient(135deg,rgba(167,139,250,.06),transparent)}

/* Market node */
.market-node .node-icon{background:rgba(0,214,143,.12);color:var(--green)}
.market-node .node-header{background:linear-gradient(135deg,rgba(0,214,143,.06),transparent)}

/* ── Bot node content ── */
.strategy-label{font-size:11px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}
.strategy-value{font-weight:600;color:var(--purple);font-size:13px}

/* ── Market node content ── */
.mkt-prices{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px}
.mkt-price-box{background:var(--bg);border-radius:6px;padding:6px 8px;text-align:center}
.mkt-price-label{font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:2px}
.mkt-price-val{font-size:15px;font-weight:700}
.mkt-price-val.up{color:var(--green)}.mkt-price-val.dn{color:var(--red)}
.mkt-info-row{display:flex;justify-content:space-between;align-items:center;padding:3px 0;font-size:12px}
.mkt-info-row .k{color:var(--muted)}.mkt-info-row .v{font-weight:600}

/* Play button */
.play-btn{width:100%;margin-top:8px;padding:8px;border:none;border-radius:8px;font:inherit;font-size:12px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:all .15s;text-transform:uppercase;letter-spacing:.5px}
.play-btn.start{background:var(--green);color:#0a0a0f}
.play-btn.start:hover{filter:brightness(1.1)}
.play-btn.running{background:rgba(0,214,143,.12);color:var(--green);border:1px solid var(--green)}
.play-btn.paused{background:rgba(255,193,69,.12);color:var(--yellow);border:1px solid var(--yellow)}

/* Connection port (the dot you drag from) */
.port{position:absolute;width:14px;height:14px;border-radius:50%;background:var(--green);border:2px solid var(--surface);cursor:crosshair;z-index:15;transition:transform .15s}
.port:hover{transform:scale(1.3)}
.port.out{right:-7px;top:50%}
.port.in{left:-7px;top:50%}

/* ── Context menu ── */
#ctx-menu{position:fixed;z-index:500;background:var(--surface2);border:1px solid var(--border);border-radius:10px;min-width:180px;padding:4px;box-shadow:0 8px 32px rgba(0,0,0,.5);display:none}
#ctx-menu.show{display:block}
.ctx-item{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:6px;cursor:pointer;font-size:13px;color:var(--text);transition:background .1s}
.ctx-item:hover{background:rgba(77,142,255,.1)}
.ctx-item svg{width:16px;height:16px;color:var(--muted)}
.ctx-sep{height:1px;background:var(--border);margin:4px 8px}
.ctx-item.danger{color:var(--red)}
.ctx-item.danger svg{color:var(--red)}

/* ── Modal (strategy picker, etc.) ── */
.modal-overlay{position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;width:380px;max-width:92vw;max-height:85vh;overflow-y:auto;box-shadow:0 16px 64px rgba(0,0,0,.5)}
.modal-header{padding:20px 20px 12px;border-bottom:1px solid var(--border)}
.modal-header h2{font-size:16px;font-weight:700}
.modal-header p{font-size:12px;color:var(--muted);margin-top:4px}
.modal-body{padding:16px 20px}
.modal-footer{padding:12px 20px 20px;display:flex;gap:8px;justify-content:flex-end}

/* Option cards (strategy/market picker) */
.option-card{display:flex;align-items:center;gap:12px;padding:12px;border:1px solid var(--border);border-radius:10px;cursor:pointer;transition:all .15s;margin-bottom:8px}
.option-card:hover{border-color:var(--blue);background:rgba(77,142,255,.05)}
.option-card.selected{border-color:var(--blue);background:rgba(77,142,255,.08)}
.option-card.disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
.option-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.option-title{font-weight:600;font-size:13px}
.option-desc{font-size:11px;color:var(--muted);margin-top:2px}

.modal-btn{border:none;border-radius:8px;font:inherit;font-size:13px;font-weight:600;padding:8px 20px;cursor:pointer;transition:all .15s}
.modal-btn.primary{background:var(--blue);color:#fff}
.modal-btn.primary:hover{filter:brightness(1.1)}
.modal-btn.ghost{background:none;color:var(--muted);border:1px solid var(--border)}
.modal-btn.ghost:hover{color:var(--text);border-color:var(--text)}

/* ── Detail panel (slide-out) ── */
#detail-panel{position:fixed;top:48px;right:0;bottom:0;width:420px;max-width:100vw;z-index:100;background:var(--surface);border-left:1px solid var(--border);transform:translateX(100%);transition:transform .25s ease;overflow-y:auto;box-shadow:-8px 0 32px rgba(0,0,0,.3)}
#detail-panel.open{transform:translateX(0)}
.dp-header{position:sticky;top:0;z-index:1;display:flex;align-items:center;gap:10px;padding:16px;background:var(--surface);border-bottom:1px solid var(--border)}
.dp-close{background:none;border:none;color:var(--muted);cursor:pointer;padding:4px;border-radius:6px}
.dp-close:hover{color:var(--text);background:var(--bg)}
.dp-title{font-weight:700;font-size:15px;flex:1}
.dp-section{padding:16px}
.dp-section+.dp-section{border-top:1px solid var(--border)}
.dp-section-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:10px;font-weight:600}
.dp-stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.dp-stat{background:var(--bg);border-radius:8px;padding:10px 12px}
.dp-stat .sl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.dp-stat .sv{font-size:18px;font-weight:700}
.dp-stat .sv.pos{color:var(--green)}.dp-stat .sv.neg{color:var(--red)}
.dp-stat.wide{grid-column:1/-1}

/* Orderbook in detail panel */
.dp-ob{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.dp-ob-panel{background:var(--bg);border-radius:8px;padding:10px}
.dp-ob-title{font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;font-weight:600}
.dp-ob-title.up{color:var(--green)}.dp-ob-title.dn{color:var(--red)}
.dp-ob-row{display:flex;justify-content:space-between;padding:2px 0;font-size:11px}
.dp-ob-row .bid{color:var(--green)}.dp-ob-row .ask{color:var(--red)}
.dp-ob-row .sz{color:var(--muted)}

/* Trade list in detail panel */
.dp-trade{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)}
.dp-trade:last-child{border-bottom:none}
.dp-trade-badge{font-size:10px;font-weight:700;padding:3px 8px;border-radius:5px;min-width:40px;text-align:center}
.dp-trade-badge.buy{background:rgba(0,214,143,.12);color:var(--green)}
.dp-trade-badge.sell{background:rgba(255,77,106,.12);color:var(--red)}
.dp-trade-info{flex:1;font-size:12px}
.dp-trade-info .side{font-weight:600}.dp-trade-info .side.up{color:var(--green)}.dp-trade-info .side.dn{color:var(--red)}
.dp-trade-right{text-align:right;font-size:12px}
.dp-trade-right .price{font-weight:600}.dp-trade-right .time{color:var(--muted);font-size:10px}

/* ── Settings panel ── */
#settings-panel{position:fixed;top:48px;right:0;bottom:0;width:380px;max-width:100vw;z-index:150;background:var(--surface);border-left:1px solid var(--border);transform:translateX(100%);transition:transform .25s ease;overflow-y:auto;box-shadow:-8px 0 32px rgba(0,0,0,.3)}
#settings-panel.open{transform:translateX(0)}

/* ── Empty state ── */
.empty-state{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;pointer-events:none}
.empty-state .icon{font-size:48px;margin-bottom:16px;opacity:.3}
.empty-state .title{font-size:16px;font-weight:600;color:var(--muted);margin-bottom:8px}
.empty-state .sub{font-size:13px;color:var(--dim)}

/* ── Activity log (left panel) ── */
#activity-log{position:fixed;top:48px;left:0;bottom:0;width:280px;z-index:90;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;transform:translateX(-100%);transition:transform .25s ease;box-shadow:8px 0 32px rgba(0,0,0,.3)}
#activity-log.open{transform:translateX(0)}
.al-header{display:flex;align-items:center;padding:12px 14px;border-bottom:1px solid var(--border);gap:8px}
.al-header .dp-title{font-size:13px}
.al-body{flex:1;overflow-y:auto;padding:4px 0}
.al-entry{display:flex;align-items:flex-start;gap:8px;padding:6px 12px;border-bottom:1px solid rgba(42,43,61,.4);font-size:11px}
.al-entry:hover{background:rgba(77,142,255,.03)}
.al-dot{width:6px;height:6px;border-radius:50%;margin-top:5px;flex-shrink:0}
.al-dot.buy{background:var(--green)}.al-dot.sell{background:var(--red)}.al-dot.info{background:var(--blue)}
.al-content{flex:1;line-height:1.4}
.al-content .al-action{font-weight:600}
.al-content .al-action.buy{color:var(--green)}.al-content .al-action.sell{color:var(--red)}
.al-content .al-detail{color:var(--muted)}
.al-time{color:var(--dim);font-size:10px;flex-shrink:0;margin-top:1px}
.slip-alert{animation:slip-flash 0.5s ease 2}
@keyframes slip-flash{0%,100%{opacity:1}50%{opacity:0.3}}
.al-toggle{position:fixed;top:56px;left:8px;z-index:91;background:var(--surface);border:1px solid var(--border);border-radius:8px;width:32px;height:32px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--muted);font-size:14px;transition:all .15s}
.al-toggle:hover{color:var(--text);border-color:var(--text)}
#activity-log.open~.al-toggle{left:288px}

/* ── PnL Summary dropdown ── */
#pnl-summary{position:fixed;top:48px;left:50%;transform:translateX(-50%);z-index:300;background:var(--surface);border:1px solid var(--border);border-radius:0 0 14px 14px;width:520px;max-width:95vw;box-shadow:0 12px 48px rgba(0,0,0,.5);display:none;max-height:70vh;overflow-y:auto}
#pnl-summary.open{display:block}
.ps-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border);margin-bottom:1px}
.ps-cell{background:var(--surface);padding:12px 14px;text-align:center}
.ps-cell .ps-label{font-size:9px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:4px}
.ps-cell .ps-val{font-size:20px;font-weight:700}
.ps-cell .ps-val.pos{color:var(--green)}.ps-cell .ps-val.neg{color:var(--red)}
.ps-cell .ps-sub{font-size:10px;color:var(--muted);margin-top:2px}
.ps-asset-row{display:flex;align-items:center;gap:12px;padding:10px 16px;border-top:1px solid var(--border)}
.ps-asset-row:hover{background:rgba(77,142,255,.03)}
.ps-asset-icon{width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex-shrink:0}
.ps-asset-info{flex:1}
.ps-asset-name{font-weight:600;font-size:12px}
.ps-asset-wdl{font-size:11px;color:var(--muted)}
.ps-asset-pnl{text-align:right;font-weight:700;font-size:14px}
.ps-sparkline{display:flex;align-items:flex-end;gap:1px;height:20px;margin-left:8px}
.ps-sparkline .bar{width:4px;border-radius:1px;min-height:1px}
.ps-sparkline .bar.pos{background:var(--green)}.ps-sparkline .bar.neg{background:var(--red)}

/* ── Bot node enhanced ── */
.bot-node{min-width:240px}
.bot-stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px}
.bot-stat{text-align:center;background:var(--bg);border-radius:6px;padding:5px 4px}
.bot-stat .bs-label{font-size:8px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.bot-stat .bs-val{font-size:14px;font-weight:700;margin-top:1px}
.bot-stat .bs-val.pos{color:var(--green)}.bot-stat .bs-val.neg{color:var(--red)}
.bot-markets{margin-top:8px}
.bot-markets-title{font-size:9px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:4px;display:flex;justify-content:space-between;align-items:center}
.bot-market-chips{display:flex;flex-wrap:wrap;gap:4px}
.bot-market-chip{display:flex;align-items:center;gap:4px;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;border:1px solid var(--border);cursor:pointer;transition:all .1s}
.bot-market-chip:hover{border-color:var(--red)}
.bot-market-chip .chip-x{color:var(--dim);font-size:8px}
.bot-market-chip:hover .chip-x{color:var(--red)}
.bot-add-market{display:flex;align-items:center;gap:4px;padding:3px 8px;border-radius:6px;font-size:10px;border:1px dashed var(--border);cursor:pointer;color:var(--muted);transition:all .1s}
.bot-add-market:hover{border-color:var(--blue);color:var(--blue)}
.bot-pnl-chart{height:40px;margin-top:8px;background:var(--bg);border-radius:6px;overflow:hidden;position:relative}
.bot-pnl-chart canvas{width:100%;height:100%}

/* ── Mobile ── */
@media(max-width:600px){
  #topbar{padding:0 10px;gap:8px}
  .top-stat{display:none}
  .top-stat:first-of-type{display:flex}
  .mode-switch button{padding:8px 12px;font-size:11px;min-height:36px}
  .top-btn{padding:8px 10px;min-height:36px}
  .node{min-width:180px}
  #detail-panel{width:100%}
  #settings-panel{width:100%}
  #activity-log{width:100%}
  #pnl-summary{width:95vw;left:2.5vw;transform:none}
  .ps-grid{grid-template-columns:1fr}
  .top-stat:nth-child(n+4){display:none}
  .al-toggle{top:56px;left:8px}
  .dp-ob{grid-template-columns:1fr}
  .ctx-item{padding:12px 16px;min-height:44px;font-size:14px}
}
</style>
</head>
<body>

<!-- Top bar -->
<div id="topbar">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v4m0 14v4M4.22 4.22l2.83 2.83m9.9 9.9l2.83 2.83M1 12h4m14 0h4M4.22 19.78l2.83-2.83m9.9-9.9l2.83-2.83"/></svg>
    PairBot
  </div>
  <div class="spacer"></div>
  <div class="top-stat" style="cursor:pointer" onclick="togglePnlSummary()"><span>PnL</span><span class="tv" id="tb-pnl">$0.00</span><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M6 9l6 6 6-6"/></svg></div>
  <div class="top-stat"><span>Balance</span><span class="tv" id="tb-balance">$0.00</span></div>
  <div class="top-stat"><span>Win Rate</span><span class="tv" id="tb-winrate">--</span></div>
  <div class="top-stat"><span>W/D/L</span><span class="tv" id="tb-wdl">0/0/0</span></div>
  <div class="top-stat"><span>Markets</span><span class="tv" id="tb-markets">0</span></div>
  <div class="mode-switch">
    <button id="mode-paper" class="active" onclick="setTradingMode('paper')">Paper</button>
    <button id="mode-live" onclick="setTradingMode('live')">Live</button>
  </div>
  <button class="top-btn" onclick="toggleSettings()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
  </button>
  <button class="top-btn" onclick="addBot()" style="color:var(--purple)">+ Bot</button>
  <button class="top-btn" onclick="addMarket()" style="color:var(--green)">+ Market</button>
  <div class="conn-dot" id="conn-dot" title="Disconnected"></div>
</div>

<!-- Workspace -->
<div id="workspace" oncontextmenu="showCtxMenu(event)">
  <canvas id="grid-canvas"></canvas>
  <div id="scene">
    <svg id="connections-svg"></svg>
  </div>
  <div class="empty-state" id="empty-hint">
    <div class="icon">+</div>
    <div class="title">Right-click to get started</div>
    <div class="sub">Add a bot and a market to begin trading</div>
  </div>
</div>

<!-- PnL Summary dropdown -->
<div id="pnl-summary">
  <div class="ps-grid">
    <div class="ps-cell"><div class="ps-label">Total PnL</div><div class="ps-val" id="ps-total">$0.00</div></div>
    <div class="ps-cell"><div class="ps-label">Win Rate</div><div class="ps-val" id="ps-winrate">--</div></div>
    <div class="ps-cell"><div class="ps-label">W / D / L</div><div class="ps-val" id="ps-wdl">0/0/0</div></div>
  </div>
  <div id="ps-assets"></div>
</div>

<!-- Activity log (left panel) -->
<div id="activity-log">
  <div class="al-header">
    <div class="dp-title">Activity Log</div>
    <div class="spacer"></div>
    <button class="dp-close" onclick="toggleActivityLog()"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
  </div>
  <div class="al-body" id="al-body"></div>
</div>
<div class="al-toggle" onclick="toggleActivityLog()" title="Activity Log">&#9776;</div>

<!-- Context menu -->
<div id="ctx-menu">
  <div class="ctx-item" onclick="addBot()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M12 8v8m-4-4h8"/></svg>
    Add Bot
  </div>
  <div class="ctx-item" onclick="addMarket()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
    Add Market
  </div>
  <div class="ctx-sep"></div>
  <div class="ctx-item danger" id="ctx-remove" style="display:none" onclick="removeSelected()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18m-2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
    Remove Selected
  </div>
</div>

<!-- Strategy picker modal -->
<div class="modal-overlay" id="modal-strategy" onclick="if(event.target===this)closeModal('modal-strategy')">
  <div class="modal">
    <div class="modal-header"><h2>Select Strategy</h2><p>Choose a trading strategy for this bot</p></div>
    <div class="modal-body">
      <div class="option-card" data-strategy="laddermate" onclick="selectStrategy(this)">
        <div class="option-icon" style="background:rgba(167,139,250,.12);color:var(--purple)">&#9881;</div>
        <div><div class="option-title">Laddermate</div><div class="option-desc">Ladder-based market making with paired positions</div></div>
      </div>
      <div class="option-card" data-strategy="mirror" onclick="selectStrategy(this)">
        <div class="option-icon" style="background:rgba(34,211,238,.12);color:var(--cyan)">&#9878;</div>
        <div><div class="option-title">Mirror</div><div class="option-desc">Mirror opposite side for balanced exposure</div></div>
      </div>
      <div class="option-card disabled" data-strategy="dutchbook" onclick="selectStrategy(this)">
        <div class="option-icon" style="background:rgba(107,109,128,.12);color:var(--muted)">&#9830;</div>
        <div><div class="option-title">Dutch Book</div><div class="option-desc">Coming soon</div></div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="modal-btn ghost" onclick="closeModal('modal-strategy')">Cancel</button>
      <button class="modal-btn primary" id="btn-create-bot" onclick="confirmBot()">Create Bot</button>
    </div>
  </div>
</div>

<!-- Market picker modal -->
<div class="modal-overlay" id="modal-market" onclick="if(event.target===this)closeModal('modal-market')">
  <div class="modal">
    <div class="modal-header"><h2>Select Market</h2><p>Choose one or more 5-minute markets</p></div>
    <div class="modal-body" id="market-options">
      <div class="option-card" data-asset="btc" onclick="toggleMarketOption(this)">
        <div class="option-icon" style="background:rgba(247,147,26,.12);color:#f7931a">&#8383;</div>
        <div><div class="option-title">BTC 5m</div><div class="option-desc">Bitcoin 5-minute up/down</div></div>
      </div>
      <div class="option-card" data-asset="eth" onclick="toggleMarketOption(this)">
        <div class="option-icon" style="background:rgba(98,126,234,.12);color:#627eea">&#926;</div>
        <div><div class="option-title">ETH 5m</div><div class="option-desc">Ethereum 5-minute up/down</div></div>
      </div>
      <div class="option-card" data-asset="sol" onclick="toggleMarketOption(this)">
        <div class="option-icon" style="background:rgba(153,69,255,.12);color:#9945ff">&#9672;</div>
        <div><div class="option-title">SOL 5m</div><div class="option-desc">Solana 5-minute up/down</div></div>
      </div>
      <div class="option-card" data-asset="xrp" onclick="toggleMarketOption(this)">
        <div class="option-icon" style="background:rgba(56,189,248,.12);color:#38bdf8">&#10005;</div>
        <div><div class="option-title">XRP 5m</div><div class="option-desc">XRP 5-minute up/down</div></div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="modal-btn ghost" onclick="closeModal('modal-market')">Cancel</button>
      <button class="modal-btn primary" onclick="confirmMarkets()">Add Markets</button>
    </div>
  </div>
</div>

<!-- Zoom controls -->
<div id="zoom-indicator">
  <button onclick="zoomBy(-0.15)">&#8722;</button>
  <span id="zoom-pct">100%</span>
  <button onclick="zoomBy(0.15)">&#43;</button>
  <button onclick="resetView()" style="margin-left:4px;width:auto;padding:0 8px;font-size:10px">Fit</button>
</div>

<!-- Detail panel (market details slide-out) -->
<div id="detail-panel">
  <div class="dp-header">
    <button class="dp-close" onclick="closeDetail()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
    <div class="dp-title" id="dp-title">Market Details</div>
    <div class="node-badge" id="dp-badge" style="background:rgba(0,214,143,.12);color:var(--green)">ACTIVE</div>
  </div>
  <!-- Strategy mode banner -->
  <div class="dp-section" style="padding:10px 16px">
    <div id="dp-mode-banner" style="text-align:center;padding:10px;border:1px solid var(--border);border-radius:8px;font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:1px">--</div>
  </div>
  <!-- Avg Sum + Position Delta -->
  <div class="dp-section">
    <div class="dp-stat-grid">
      <div class="dp-stat">
        <div class="sl">AVG SUM</div>
        <div class="sv" id="dp-avg-sum" style="font-size:24px">--</div>
        <div id="dp-avg-sum-sub" style="font-size:11px;color:var(--muted);margin-top:2px">--</div>
      </div>
      <div class="dp-stat">
        <div class="sl">POSITION &Delta;</div>
        <div class="sv" id="dp-delta" style="font-size:24px">0.0%</div>
        <div id="dp-delta-sub" style="font-size:11px;color:var(--muted);margin-top:2px">--</div>
      </div>
    </div>
  </div>
  <!-- If Down/Up Wins -->
  <div class="dp-section">
    <div class="dp-stat-grid">
      <div class="dp-stat">
        <div class="sl" style="display:flex;align-items:center;gap:4px"><span style="color:var(--red)">&searr;</span> IF DOWN WINS</div>
        <div class="sv" id="dp-if-dn" style="font-size:20px">--</div>
      </div>
      <div class="dp-stat">
        <div class="sl" style="display:flex;align-items:center;gap:4px"><span style="color:var(--green)">&nearr;</span> IF UP WINS</div>
        <div class="sv" id="dp-if-up" style="font-size:20px">--</div>
      </div>
    </div>
    <div style="display:flex;justify-content:space-between;padding:8px 4px 0;font-size:12px;color:var(--muted)">
      <span>Worst: <strong id="dp-worst" style="color:var(--red)">--</strong></span>
      <span>Best: <strong id="dp-best-pnl" style="color:var(--green)">--</strong></span>
    </div>
  </div>
  <!-- Market Prices -->
  <div class="dp-section">
    <div class="dp-section-title" style="display:flex;justify-content:space-between;align-items:center">
      <span>MARKET PRICES</span>
      <span style="font-size:11px;font-weight:400">
        <span style="color:var(--green)">UP</span> <span id="dp-up-bid">--</span> / <span id="dp-up-ask">--</span>
        &nbsp;&middot;&nbsp;
        <span style="color:var(--red)">DN</span> <span id="dp-dn-bid">--</span> / <span id="dp-dn-ask">--</span>
      </span>
    </div>
    <div class="dp-ob">
      <div class="dp-ob-panel"><div class="dp-ob-title up">UP Token</div><div id="dp-ob-up">--</div></div>
      <div class="dp-ob-panel"><div class="dp-ob-title dn">DOWN Token</div><div id="dp-ob-dn">--</div></div>
    </div>
  </div>
  <!-- Cumulative Shares -->
  <div class="dp-section">
    <div class="dp-section-title" style="display:flex;justify-content:space-between;align-items:center">
      <span>CUMULATIVE SHARES</span>
      <span style="font-size:11px;font-weight:400">
        <span style="color:var(--green)">UP</span> <strong id="dp-qty-up">0</strong> @<span id="dp-avg-up">--</span>
        &nbsp;&middot;&nbsp;
        <span style="color:var(--red)">DN</span> <strong id="dp-qty-dn">0</strong> @<span id="dp-avg-dn">--</span>
      </span>
    </div>
    <div class="dp-stat-grid">
      <div class="dp-stat"><div class="sl">Trade Count</div><div class="sv" id="dp-trades">0</div></div>
      <div class="dp-stat"><div class="sl">Mode</div><div class="sv" id="dp-mode" style="font-size:13px">--</div></div>
    </div>
  </div>
  <!-- Recent Trades -->
  <div class="dp-section">
    <div class="dp-section-title">Recent Trades</div>
    <div id="dp-trade-list"><div style="color:var(--muted);font-size:12px">No trades yet</div></div>
  </div>
</div>

<!-- Settings slide-out -->
<div id="settings-panel">
  <div class="dp-header">
    <button class="dp-close" onclick="toggleSettings()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
    <div class="dp-title">Settings</div>
  </div>
  <div class="dp-section" id="settings-body">
    <div class="dp-section-title">Credentials</div>
    <p style="color:var(--muted);font-size:12px;">Configure via the settings API endpoint.</p>
  </div>
</div>

<script>
// ─── State ───────────────────────────────────────────────────────────────────
let ws, nodes = [], connections = [], nextId = 1, selectedNode = null;
let dragging = null, dragOff = {x:0,y:0};
let panX = 0, panY = 0, zoom = 1;
let isPanning = false, panStart = {x:0,y:0}, panStartPan = {x:0,y:0};
let connecting = null; // {fromId}
let connectMouseScreen = null; // current mouse in screen coords while connecting
let ctxPos = {x:0, y:0}; // screen coords for context menu placement
let latestData = null;
let pendingStrategy = null;

const ASSET_COLORS = {btc:'#f7931a',eth:'#627eea',sol:'#9945ff',xrp:'#38bdf8'};
const ASSET_ICONS = {btc:'\u20BF',eth:'\u039E',sol:'\u25C8',xrp:'\u2715'};
const MIN_ZOOM = 0.25, MAX_ZOOM = 2.5;
const scene = () => document.getElementById('scene');

// ─── Coordinate helpers ─────────────────────────────────────────────────────
// Screen (client) coords → scene (world) coords
function screenToWorld(sx, sy) {
  return { x: (sx - panX) / zoom, y: (sy - 48 - panY) / zoom };
}
// Scene (world) coords → screen coords (relative to workspace top-left)
function worldToScreen(wx, wy) {
  return { x: wx * zoom + panX, y: wy * zoom + panY };
}

function applyTransform() {
  scene().style.transform = `translate(${panX}px,${panY}px) scale(${zoom})`;
  document.getElementById('zoom-pct').textContent = Math.round(zoom * 100) + '%';
  drawGrid();
  drawConnections();
}

// ─── Zoom ────────────────────────────────────────────────────────────────────
function zoomAt(delta, cx, cy) {
  const oldZoom = zoom;
  zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom + delta));
  // Keep the point under the cursor stable
  const ratio = zoom / oldZoom;
  panX = cx - (cx - panX) * ratio;
  panY = (cy - 48) - ((cy - 48) - panY) * ratio;
  applyTransform();
}
function zoomBy(delta) {
  zoomAt(delta, innerWidth / 2, innerHeight / 2 + 48);
}
function resetView() {
  zoom = 1; panX = 0; panY = 0;
  applyTransform();
}

// Mouse wheel zoom
document.getElementById('workspace').addEventListener('wheel', e => {
  e.preventDefault();
  const delta = -e.deltaY * 0.001;
  zoomAt(delta, e.clientX, e.clientY);
}, {passive: false});

// Pinch zoom (touch)
let lastPinchDist = 0, lastPinchCenter = null;
document.getElementById('workspace').addEventListener('touchstart', e => {
  if (e.touches.length === 2) {
    e.preventDefault();
    const dx = e.touches[0].clientX - e.touches[1].clientX;
    const dy = e.touches[0].clientY - e.touches[1].clientY;
    lastPinchDist = Math.hypot(dx, dy);
    lastPinchCenter = {
      x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
      y: (e.touches[0].clientY + e.touches[1].clientY) / 2
    };
  }
}, {passive: false});

// ─── Grid ────────────────────────────────────────────────────────────────────
function drawGrid(){
  const c = document.getElementById('grid-canvas');
  const ctx = c.getContext('2d');
  const dpr = devicePixelRatio || 1;
  const w = innerWidth, h = innerHeight - 48;
  c.width = w * dpr; c.height = h * dpr;
  c.style.width = w + 'px'; c.style.height = h + 'px';
  ctx.scale(dpr, dpr);
  ctx.fillStyle = 'rgba(77,142,255,.06)';
  const baseGap = 30;
  const gap = baseGap * zoom;
  const ox = (panX % gap + gap) % gap;
  const oy = (panY % gap + gap) % gap;
  const dotSize = Math.max(1, 1.5 * zoom);
  for(let x = ox; x < w; x += gap)
    for(let y = oy; y < h; y += gap)
      ctx.fillRect(x, y, dotSize, dotSize);
}
try{drawGrid()}catch(e){console.error('drawGrid failed:',e)}
addEventListener('resize', () => { drawGrid(); applyTransform(); });

// ─── Node management ─────────────────────────────────────────────────────────
function createNodeEl(node) {
  const el = document.createElement('div');
  el.className = `node ${node.type}-node`;
  el.id = `node-${node.id}`;
  el.style.left = node.x + 'px';
  el.style.top = node.y + 'px';

  if (node.type === 'bot') {
    el.innerHTML = `
      <div class="node-header" onmousedown="startDrag(event,${node.id})" ontouchstart="startDragTouch(event,${node.id})">
        <div class="node-icon">\u2699</div>
        <div class="node-title">${node.name}</div>
        <div class="node-badge" style="background:rgba(167,139,250,.12);color:var(--purple)">${node.strategy.toUpperCase()}</div>
      </div>
      <div class="node-body">
        <div class="bot-stats">
          <div class="bot-stat"><div class="bs-label">PnL</div><div class="bs-val" id="bp-pnl-${node.id}">$0.00</div></div>
          <div class="bot-stat"><div class="bs-label">W/D/L</div><div class="bs-val" id="bp-wdl-${node.id}">0/0/0</div></div>
          <div class="bot-stat"><div class="bs-label">Trades</div><div class="bs-val" id="bp-trades-${node.id}">0</div></div>
        </div>
        <div class="bot-pnl-chart"><canvas id="bp-chart-${node.id}"></canvas></div>
        <div class="bot-markets">
          <div class="bot-markets-title"><span>Markets</span><span class="bot-add-market" onclick="event.stopPropagation();addMarketToBot(${node.id})">+ Add</span></div>
          <div class="bot-market-chips" id="bp-chips-${node.id}"></div>
        </div>
      </div>
      <div class="port out" onmousedown="startConnect(event,${node.id})" ontouchstart="startConnectTouch(event,${node.id})"></div>`;
  } else {
    const asset = node.asset.toUpperCase();
    const color = ASSET_COLORS[node.asset] || '#fff';
    el.innerHTML = `
      <div class="node-header" onmousedown="startDrag(event,${node.id})" ontouchstart="startDragTouch(event,${node.id})">
        <div class="node-icon" style="background:${color}22;color:${color}">${ASSET_ICONS[node.asset]||'?'}</div>
        <div class="node-title">${asset} 5-min Market</div>
        <div class="node-badge" id="mkt-badge-${node.id}" style="background:rgba(0,214,143,.12);color:var(--green)">SCANNING</div>
      </div>
      <div class="node-body" onclick="openDetail('${node.asset}')">
        <div class="mkt-prices">
          <div class="mkt-price-box"><div class="mkt-price-label">UP</div><div class="mkt-price-val up" id="mp-up-${node.id}">--</div></div>
          <div class="mkt-price-box"><div class="mkt-price-label">DOWN</div><div class="mkt-price-val dn" id="mp-dn-${node.id}">--</div></div>
        </div>
        <div class="mkt-info-row"><span class="k">Shares</span><span class="v" id="mp-shares-${node.id}" style="font-size:11px">--</span></div>
        <div class="mkt-info-row"><span class="k">Time left</span><span class="v" id="mp-time-${node.id}">--:--</span></div>
        <div class="mkt-info-row"><span class="k">PnL</span><span class="v" id="mp-pnl-${node.id}" style="font-weight:700">$0.00</span></div>
        <div class="mkt-info-row"><span class="k">Worst / Best</span><span class="v" id="mp-wb-${node.id}" style="font-size:11px">-- / --</span></div>
        <div class="mkt-info-row"><span class="k">Trades</span><span class="v" id="mp-trades-${node.id}">0</span></div>
        <div id="mp-slippage-${node.id}" style="display:none;margin-top:4px;padding:4px 6px;background:rgba(255,77,106,.08);border:1px solid rgba(255,77,106,.2);border-radius:6px;font-size:10px;color:var(--red)"></div>
        <button class="play-btn start" id="mp-play-${node.id}" onclick="event.stopPropagation();toggleMarketPlay('${node.asset}')">&#9654; Start</button>
      </div>
      <div class="port in" onmousedown="event.stopPropagation()" ontouchstart="event.stopPropagation()"></div>`;
  }
  el.addEventListener('click', (e) => { e.stopPropagation(); selectNode(node.id); });
  scene().appendChild(el);  // append to scene, not workspace
  updateEmptyState();
}

function updateEmptyState() {
  document.getElementById('empty-hint').style.display = nodes.length ? 'none' : '';
}

// ─── Drag nodes ──────────────────────────────────────────────────────────────
function startDrag(e, id) {
  if (e.button !== 0) return;
  e.preventDefault();
  const node = nodes.find(n=>n.id===id);
  if (!node) return;
  dragging = node;
  // Offset in world space
  const world = screenToWorld(e.clientX, e.clientY);
  dragOff = {x: world.x - node.x, y: world.y - node.y};
  selectNode(id);
}
function startDragTouch(e, id) {
  e.preventDefault();
  const t = e.touches[0];
  const node = nodes.find(n=>n.id===id);
  if (!node) return;
  dragging = node;
  const world = screenToWorld(t.clientX, t.clientY);
  dragOff = {x: world.x - node.x, y: world.y - node.y};
  selectNode(id);
}

document.addEventListener('mousemove', e => {
  // Panning
  if (isPanning) {
    panX = panStartPan.x + (e.clientX - panStart.x);
    panY = panStartPan.y + (e.clientY - panStart.y);
    applyTransform();
    return;
  }
  // Dragging a node
  if (dragging) {
    const world = screenToWorld(e.clientX, e.clientY);
    dragging.x = world.x - dragOff.x;
    dragging.y = world.y - dragOff.y;
    const el = document.getElementById(`node-${dragging.id}`);
    el.style.left = dragging.x + 'px';
    el.style.top = dragging.y + 'px';
    drawConnections();
  }
  // Drawing connection
  if (connecting) {
    connectMouseScreen = {x: e.clientX, y: e.clientY};
    drawConnections();
  }
});

document.addEventListener('mouseup', e => {
  if (connecting) { finishConnect(e); }
  dragging = null;
  isPanning = false;
  document.getElementById('workspace').style.cursor = 'grab';
});

document.addEventListener('touchmove', e => {
  // 2-finger pinch/pan
  if (e.touches.length === 2 && lastPinchCenter) {
    e.preventDefault();
    const dx = e.touches[0].clientX - e.touches[1].clientX;
    const dy = e.touches[0].clientY - e.touches[1].clientY;
    const dist = Math.hypot(dx, dy);
    const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
    const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    // Pinch zoom
    const scale = dist / lastPinchDist;
    zoomAt((scale - 1) * zoom, cx, cy);
    // Pan
    panX += cx - lastPinchCenter.x;
    panY += cy - lastPinchCenter.y;
    lastPinchDist = dist;
    lastPinchCenter = {x: cx, y: cy};
    applyTransform();
    return;
  }
  const t = e.touches[0];
  if (dragging) {
    e.preventDefault();
    const world = screenToWorld(t.clientX, t.clientY);
    dragging.x = world.x - dragOff.x;
    dragging.y = world.y - dragOff.y;
    const el = document.getElementById(`node-${dragging.id}`);
    el.style.left = dragging.x + 'px';
    el.style.top = dragging.y + 'px';
    drawConnections();
  }
  if (connecting) {
    connectMouseScreen = {x: t.clientX, y: t.clientY};
    drawConnections();
  }
}, {passive:false});

document.addEventListener('touchend', e => {
  if (connecting && e.changedTouches.length) {
    const t = e.changedTouches[0];
    finishConnect({clientX: t.clientX, clientY: t.clientY});
  }
  dragging = null;
  lastPinchCenter = null;
});

// Middle-click or space+drag to pan
document.getElementById('workspace').addEventListener('mousedown', e => {
  // Middle mouse button, or clicking on the empty workspace (not on a node)
  const onNode = e.target.closest('.node');
  if (e.button === 1 || (!onNode && e.button === 0 && !connecting)) {
    isPanning = true;
    panStart = {x: e.clientX, y: e.clientY};
    panStartPan = {x: panX, y: panY};
    document.getElementById('workspace').style.cursor = 'grabbing';
    e.preventDefault();
  }
});

function selectNode(id) {
  selectedNode = id;
  document.querySelectorAll('.node').forEach(n=>n.classList.remove('selected'));
  const el = document.getElementById(`node-${id}`);
  if (el) el.classList.add('selected');
  document.getElementById('ctx-remove').style.display = id ? '' : 'none';
}

// ─── Connections ─────────────────────────────────────────────────────────────
// Get port position in WORLD coords
function getPortWorldPos(nodeId, portType) {
  const el = document.getElementById(`node-${nodeId}`);
  if (!el) return null;
  const port = el.querySelector(`.port.${portType}`);
  if (!port) return null;
  // Port center relative to node
  const nodeRect = el.getBoundingClientRect();
  const portRect = port.getBoundingClientRect();
  const node = nodes.find(n => n.id === nodeId);
  if (!node) return null;
  // The node's x,y is in world coords, the port offset is relative to the node
  const portOffX = (portRect.left + portRect.width/2 - nodeRect.left) / zoom;
  const portOffY = (portRect.top + portRect.height/2 - nodeRect.top) / zoom;
  return { x: node.x + portOffX, y: node.y + portOffY };
}

function startConnect(e, fromId) {
  e.preventDefault(); e.stopPropagation();
  connecting = {fromId};
  connectMouseScreen = {x: e.clientX, y: e.clientY};
  document.getElementById('workspace').classList.add('connecting');
}
function startConnectTouch(e, fromId) {
  e.preventDefault(); e.stopPropagation();
  const t = e.touches[0];
  connecting = {fromId};
  connectMouseScreen = {x: t.clientX, y: t.clientY};
  document.getElementById('workspace').classList.add('connecting');
}

function finishConnect(e) {
  document.getElementById('workspace').classList.remove('connecting');
  if (!connecting) return;
  // Find if we're over a market node
  const target = document.elementFromPoint(e.clientX, e.clientY);
  const marketNode = target?.closest('.market-node');
  if (marketNode) {
    const toId = parseInt(marketNode.id.replace('node-',''));
    const exists = connections.find(c=>c.from===connecting.fromId && c.to===toId);
    if (!exists && toId !== connecting.fromId) {
      connections.push({from: connecting.fromId, to: toId});
    }
  }
  connecting = null;
  connectMouseScreen = null;
  drawConnections();
}

function drawConnections() {
  const svg = document.getElementById('connections-svg');
  let html = '';
  // Draw established connections (in world coords — SVG is inside #scene)
  for (const conn of connections) {
    const fromPos = getPortWorldPos(conn.from, 'out');
    const toPos = getPortWorldPos(conn.to, 'in');
    if (!fromPos || !toPos) continue;
    const x1 = fromPos.x, y1 = fromPos.y;
    const x2 = toPos.x, y2 = toPos.y;
    const dx = Math.abs(x2 - x1) * 0.5;
    html += `<path d="M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}"/>`;
  }
  // Draw temp connection while dragging
  if (connecting && connectMouseScreen) {
    const fromPos = getPortWorldPos(connecting.fromId, 'out');
    if (fromPos) {
      const x1 = fromPos.x, y1 = fromPos.y;
      // Convert current mouse screen pos to world
      const w = screenToWorld(connectMouseScreen.x, connectMouseScreen.y);
      const x2 = w.x, y2 = w.y;
      const dx = Math.abs(x2 - x1) * 0.5;
      html += `<path class="temp" d="M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}"/>`;
    }
  }
  svg.innerHTML = html;
}

// ─── Context menu ────────────────────────────────────────────────────────────
function showCtxMenu(e) {
  e.preventDefault();
  const menu = document.getElementById('ctx-menu');
  // Store screen position for menu placement AND for node creation (convert to world)
  ctxPos = {x: e.clientX, y: e.clientY};
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.classList.add('show');
  document.getElementById('ctx-remove').style.display = selectedNode ? '' : 'none';
}
document.addEventListener('click', () => document.getElementById('ctx-menu').classList.remove('show'));

// ─── Add bot ─────────────────────────────────────────────────────────────────
function addBot() {
  ctxPos = {x: innerWidth/2 - 100, y: innerHeight/2};
  document.getElementById('ctx-menu').classList.remove('show');
  document.querySelectorAll('#modal-strategy .option-card').forEach(c=>c.classList.remove('selected'));
  pendingStrategy = null;
  document.getElementById('modal-strategy').classList.add('open');
}
function selectStrategy(el) {
  if (el.classList.contains('disabled')) return;
  document.querySelectorAll('#modal-strategy .option-card').forEach(c=>c.classList.remove('selected'));
  el.classList.add('selected');
  pendingStrategy = el.dataset.strategy;
}
function confirmBot() {
  if (!pendingStrategy) return;
  const w = screenToWorld(ctxPos.x, ctxPos.y);
  const node = {id: nextId++, type:'bot', name:`Bot ${nextId-1}`, strategy: pendingStrategy, x: w.x, y: w.y};
  nodes.push(node);
  createNodeEl(node);
  closeModal('modal-strategy');
}

// ─── Add market ──────────────────────────────────────────────────────────────
function addMarket() {
  ctxPos = {x: innerWidth/2 + 100, y: innerHeight/2};
  document.getElementById('ctx-menu').classList.remove('show');
  document.querySelectorAll('#modal-market .option-card').forEach(c=>c.classList.remove('selected'));
  // Disable already-added markets
  document.querySelectorAll('#modal-market .option-card').forEach(c => {
    const asset = c.dataset.asset;
    const exists = nodes.find(n=>n.type==='market'&&n.asset===asset);
    if (exists) { c.classList.add('disabled'); c.style.opacity='.3'; }
    else { c.classList.remove('disabled'); c.style.opacity=''; }
  });
  document.getElementById('modal-market').classList.add('open');
}
function toggleMarketOption(el) {
  if (el.classList.contains('disabled')) return;
  el.classList.toggle('selected');
}
function confirmMarkets() {
  const selected = [...document.querySelectorAll('#modal-market .option-card.selected')].map(c=>c.dataset.asset);
  let offset = 0;
  for (const asset of selected) {
    if (nodes.find(n=>n.type==='market'&&n.asset===asset)) continue;
    const w = screenToWorld(ctxPos.x, ctxPos.y);
    const node = {id: nextId++, type:'market', asset, x: w.x + offset, y: w.y + offset};
    nodes.push(node);
    createNodeEl(node);
    offset += 40;
  }
  closeModal('modal-market');
  // Tell backend about assets
  syncAssets();
}

// ─── Remove ──────────────────────────────────────────────────────────────────
function removeSelected() {
  document.getElementById('ctx-menu').classList.remove('show');
  if (!selectedNode) return;
  // Remove connections involving this node
  connections = connections.filter(c=>c.from!==selectedNode&&c.to!==selectedNode);
  // Remove node
  nodes = nodes.filter(n=>n.id!==selectedNode);
  const el = document.getElementById(`node-${selectedNode}`);
  if (el) el.remove();
  selectedNode = null;
  drawConnections();
  updateEmptyState();
  syncAssets();
}

// ─── Modals ──────────────────────────────────────────────────────────────────
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

// ─── Detail panel ────────────────────────────────────────────────────────────
let openAsset = null;
function openDetail(asset) {
  openAsset = asset;
  document.getElementById('dp-title').textContent = asset.toUpperCase() + ' 5-min Market';
  document.getElementById('detail-panel').classList.add('open');
  updateDetailPanel();
}
function closeDetail() {
  document.getElementById('detail-panel').classList.remove('open');
  openAsset = null;
}

function renderDpTrade(t, isCurrent) {
  const act = (t.action||'BUY').toString();
  const isBuy = act === 'BUY';
  const isSell = act === 'SELL';
  const badgeClass = isSell ? 'sell' : 'buy';
  const badgeText = isSell ? 'SELL' : isBuy ? 'BUY' : act.replace(/_/g,' ');
  const sideClass = t.side === 'UP' ? 'up' : 'dn';
  const profitHtml = (isSell && typeof t.profit === 'number')
    ? `<div style="font-size:11px;font-weight:600;color:${t.profit>=0?'var(--green)':'var(--red)'}">${t.profit>=0?'+':''}$${t.profit.toFixed(3)}</div>` : '';
  const borderStyle = isCurrent ? 'border-left:2px solid var(--blue);padding-left:8px;margin-left:-8px' : '';
  return `<div class="dp-trade" style="${borderStyle}">
    <div class="dp-trade-badge ${badgeClass}">${badgeText}</div>
    <div class="dp-trade-info"><span class="side ${sideClass}">${t.side}</span> &times; ${t.qty.toFixed(1)}</div>
    <div class="dp-trade-right"><div class="price">$${t.price.toFixed(3)}</div>${profitHtml}<div class="time">${t.time||''}</div></div>
  </div>`;
}
function updateDetailPanel() {
  if (!openAsset || !latestData?.active_markets) return;
  let mData = null;
  for (const [slug, d] of Object.entries(latestData.active_markets)) {
    if (d.asset === openAsset) { mData = d; break; }
  }
  if (!mData) return;
  const pt = mData.paper_trader || {};
  const pnlStr = v => (v >= 0 ? '+' : '') + '$' + v.toFixed(2);
  const pnlCls = v => v >= 0 ? 'pos' : 'neg';

  // Mode banner
  const banner = document.getElementById('dp-mode-banner');
  if (banner) {
    const mode = pt.current_mode || '--';
    banner.textContent = mode;
    banner.style.borderColor = mode === 'arb' ? 'var(--green)' : mode === 'holding' ? 'var(--yellow)' : 'var(--border)';
    banner.style.color = mode === 'arb' ? 'var(--green)' : mode === 'holding' ? 'var(--yellow)' : 'var(--muted)';
  }

  // Avg Sum
  const avgSum = pt.pair_cost || 0;
  const sumEl = document.getElementById('dp-avg-sum');
  if (sumEl) {
    sumEl.textContent = avgSum > 0 ? avgSum.toFixed(4) : '--';
    sumEl.className = 'sv ' + (avgSum > 0 && avgSum < 1.0 ? 'pos' : avgSum >= 1.0 ? 'neg' : '');
  }
  const sumSub = document.getElementById('dp-avg-sum-sub');
  if (sumSub) {
    if (avgSum > 0 && avgSum < 1.0) {
      sumSub.innerHTML = '<span style="color:var(--green)">' + ((1 - avgSum) * 100).toFixed(2) + '% profit</span>';
    } else if (avgSum >= 1.0) {
      sumSub.innerHTML = '<span style="color:var(--red)">' + ((avgSum - 1) * 100).toFixed(2) + '% loss</span>';
    } else {
      sumSub.textContent = '--';
    }
  }

  // Position Delta
  const delta = pt.balance_pct || 0;
  const deltaEl = document.getElementById('dp-delta');
  if (deltaEl) {
    deltaEl.textContent = delta.toFixed(1) + '%';
    deltaEl.className = 'sv ' + (delta <= 3 ? 'pos' : 'neg');
  }
  const deltaSub = document.getElementById('dp-delta-sub');
  if (deltaSub) deltaSub.textContent = delta <= 3 ? 'Balanced' : 'Imbalanced';

  // If Down/Up Wins
  const pnlDn = pt.pnl_if_down_wins ?? (pt.qty_down - (pt.cost_up||0) - (pt.cost_down||0));
  const pnlUp = pt.pnl_if_up_wins ?? (pt.qty_up - (pt.cost_up||0) - (pt.cost_down||0));
  const ifDn = document.getElementById('dp-if-dn');
  const ifUp = document.getElementById('dp-if-up');
  if (ifDn) { ifDn.textContent = pnlStr(pnlDn); ifDn.className = 'sv ' + pnlCls(pnlDn); }
  if (ifUp) { ifUp.textContent = pnlStr(pnlUp); ifUp.className = 'sv ' + pnlCls(pnlUp); }
  const worst = Math.min(pnlDn, pnlUp), best = Math.max(pnlDn, pnlUp);
  const worstEl = document.getElementById('dp-worst');
  const bestEl = document.getElementById('dp-best-pnl');
  if (worstEl) worstEl.textContent = pnlStr(worst);
  if (bestEl) bestEl.textContent = pnlStr(best);

  // Market Prices (bid/ask)
  const obs = mData.orderbooks || {};
  const upBids = obs.up?.bids || [], upAsks = obs.up?.asks || [];
  const dnBids = obs.down?.bids || [], dnAsks = obs.down?.asks || [];
  const fmt = v => v ? Number(v.price || v).toFixed(2) : '--';
  const el = id => document.getElementById(id);
  if (el('dp-up-bid')) el('dp-up-bid').textContent = upBids[0] ? fmt(upBids[0]) : '--';
  if (el('dp-up-ask')) el('dp-up-ask').textContent = upAsks[0] ? fmt(upAsks[0]) : '--';
  if (el('dp-dn-bid')) el('dp-dn-bid').textContent = dnBids[0] ? fmt(dnBids[0]) : '--';
  if (el('dp-dn-ask')) el('dp-dn-ask').textContent = dnAsks[0] ? fmt(dnAsks[0]) : '--';
  renderDetailOB('dp-ob-up', obs.up);
  renderDetailOB('dp-ob-dn', obs.down);

  // Cumulative Shares
  if (el('dp-qty-up')) el('dp-qty-up').textContent = (pt.qty_up||0).toFixed(1);
  if (el('dp-qty-dn')) el('dp-qty-dn').textContent = (pt.qty_down||0).toFixed(1);
  if (el('dp-avg-up')) el('dp-avg-up').textContent = pt.avg_up ? pt.avg_up.toFixed(4) : '--';
  if (el('dp-avg-dn')) el('dp-avg-dn').textContent = pt.avg_down ? pt.avg_down.toFixed(4) : '--';
  if (el('dp-trades')) el('dp-trades').textContent = pt.trade_count || 0;
  if (el('dp-mode')) el('dp-mode').textContent = pt.current_mode || '--';

  // Trades — find active market slug for this asset
  let activeSlug = null;
  if (latestData.active_markets) {
    for (const [slug, d] of Object.entries(latestData.active_markets)) {
      if (d.asset === openAsset && d.paper_trader?.market_status !== 'resolved') { activeSlug = slug; break; }
    }
  }
  const allTrades = (latestData.trade_log || []).filter(t => t.asset.toLowerCase() === openAsset.toLowerCase());
  // Split: current market vs older
  const currentTrades = activeSlug ? allTrades.filter(t => t.market === activeSlug) : [];
  const olderTrades = activeSlug ? allTrades.filter(t => t.market !== activeSlug) : allTrades;
  const tradeListEl = document.getElementById('dp-trade-list');
  let html = '';
  if (currentTrades.length) {
    html += '<div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--blue);margin-bottom:6px;display:flex;align-items:center;gap:4px"><span style="width:6px;height:6px;border-radius:50%;background:var(--blue)"></span>Current Market</div>';
    html += currentTrades.slice(-10).reverse().map(t => renderDpTrade(t, true)).join('');
  }
  if (olderTrades.length) {
    if (currentTrades.length) html += '<div style="font-size:10px;color:var(--dim);margin:8px 0 6px;padding-top:8px;border-top:1px solid var(--border)">Previous Markets</div>';
    html += olderTrades.slice(-5).reverse().map(t => renderDpTrade(t, false)).join('');
  }
  if (!currentTrades.length && !olderTrades.length) {
    html = '<div style="color:var(--muted);font-size:12px">No trades yet</div>';
  }
  tradeListEl.innerHTML = html;
}

function renderDetailOB(elId, book) {
  const el = document.getElementById(elId);
  if (!book || (!book.bids?.length && !book.asks?.length)) { el.innerHTML='<div style="color:var(--muted);font-size:11px">No data</div>'; return; }
  let h = '';
  (book.asks||[]).slice(0,5).reverse().forEach(l => {
    h += `<div class="dp-ob-row"><span class="ask">${Number(l.price).toFixed(3)}</span><span class="sz">${Number(l.size).toFixed(0)}</span></div>`;
  });
  h += '<div style="border-top:1px solid var(--border);margin:4px 0"></div>';
  (book.bids||[]).slice(0,5).forEach(l => {
    h += `<div class="dp-ob-row"><span class="bid">${Number(l.price).toFixed(3)}</span><span class="sz">${Number(l.size).toFixed(0)}</span></div>`;
  });
  el.innerHTML = h;
}

// ─── Settings ────────────────────────────────────────────────────────────────
function toggleSettings() {
  document.getElementById('settings-panel').classList.toggle('open');
}

// ─── Trading mode ────────────────────────────────────────────────────────────
function setTradingMode(mode) {
  document.getElementById('mode-paper').classList.toggle('active', mode==='paper');
  document.getElementById('mode-live').classList.toggle('active', mode==='live');
  // Save setting to backend
  fetch('/api/settings', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({live_trading: mode==='live'})
  });
  // Arm/disarm live execution via WebSocket, and ensure bot is running
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (mode === 'live') {
      ws.send(JSON.stringify({action: 'arm_live'}));
      ws.send(JSON.stringify({action: 'start_current_market'}));
      // Ensure bot is unpaused after settings POST completes
      ensureRunningDelayed();
    } else {
      ws.send(JSON.stringify({action: 'disarm_live'}));
    }
  }
}

// ─── Market play/pause ──────────────────────────────────────────────────────
function toggleMarketPlay(asset) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({action:'pause'})); // toggle pause/resume
  }
}
// Ensure bot is running (not paused)
function ensureRunning() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  // Always send pause toggle if latest state says paused
  if (latestData?.paused === true) {
    ws.send(JSON.stringify({action: 'pause'}));
  }
}
// Poll for first state then unpause if needed
function ensureRunningDelayed() {
  let checks = 0;
  const iv = setInterval(() => {
    if (++checks > 10) { clearInterval(iv); return; }
    if (latestData?.paused !== undefined) {
      clearInterval(iv);
      ensureRunning();
    }
  }, 300);
}

// ─── Sync assets to backend ──────────────────────────────────────────────────
function isLiveMode() {
  return document.getElementById('mode-live').classList.contains('active');
}
function syncAssets() {
  const assets = nodes.filter(n=>n.type==='market').map(n=>n.asset);
  if (!assets.length) return;
  fetch('/api/settings', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({assets, live_trading: isLiveMode()})
  });
}

// ─── Activity log ────────────────────────────────────────────────────────────
let activityLogOpen = false;
function toggleActivityLog() {
  activityLogOpen = !activityLogOpen;
  document.getElementById('activity-log').classList.toggle('open', activityLogOpen);
}
let lastTradeCount = 0;
function updateActivityLog(data) {
  const trades = data.trade_log;
  if (!trades || trades.length === lastTradeCount) return;
  lastTradeCount = trades.length;
  const el = document.getElementById('al-body');

  // Build set of current active market slugs
  const activeSlugs = new Set();
  if (data.active_markets) {
    for (const [slug, d] of Object.entries(data.active_markets)) {
      if (d.paper_trader?.market_status !== 'resolved') activeSlugs.add(slug);
    }
  }

  // Show last 50 trades, newest first
  const recent = trades.slice(-50).reverse();
  el.innerHTML = recent.map(t => {
    const act = (t.action||'BUY').toString();
    const isSell = act === 'SELL';
    const dotClass = isSell ? 'sell' : (act.startsWith('QUOTE') ? 'info' : 'buy');
    const actionClass = isSell ? 'sell' : 'buy';
    const label = isSell ? 'SOLD' : act === 'BUY' ? 'BOUGHT' : act.replace(/_/g,' ');
    const profitStr = (isSell && typeof t.profit === 'number')
      ? ` <span style="color:${t.profit>=0?'var(--green)':'var(--red)'}">${t.profit>=0?'+':''}$${t.profit.toFixed(3)}</span>` : '';
    // Highlight trades from current active market
    const isCurrentMkt = t.market && activeSlugs.has(t.market);
    const currentTag = isCurrentMkt ? '<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--blue);margin-left:4px;vertical-align:middle" title="Current market"></span>' : '';
    const entryStyle = isCurrentMkt ? 'border-left:2px solid var(--blue);padding-left:10px' : '';
    // Slippage detection for sells
    let slipTag = '';
    if (isSell && typeof t.profit === 'number' && t.profit < -0.20) {
      slipTag = ' <span style="background:rgba(255,77,106,.15);padding:1px 4px;border-radius:3px;font-size:9px;color:var(--red)">SLIP</span>';
    }
    return `<div class="al-entry" style="${entryStyle}">
      <div class="al-dot ${dotClass}"></div>
      <div class="al-content">
        <span class="al-action ${actionClass}">${label}</span> ${t.qty.toFixed(1)} ${t.side} <span style="color:${ASSET_COLORS[t.asset.toLowerCase()]||'var(--text)'};font-weight:600">${t.asset}</span> @ $${t.price.toFixed(3)}${profitStr}${slipTag}${currentTag}
        <div class="al-detail">pair cost: $${(t.pair_cost||0).toFixed(3)}</div>
      </div>
      <div class="al-time">${t.time||''}</div>
    </div>`;
  }).join('');
}

// ─── PnL Summary ─────────────────────────────────────────────────────────────
function togglePnlSummary() {
  document.getElementById('pnl-summary').classList.toggle('open');
}
// Close on outside click
document.addEventListener('click', e => {
  if (!e.target.closest('#pnl-summary') && !e.target.closest('.top-stat')) {
    document.getElementById('pnl-summary').classList.remove('open');
  }
});

function updatePnlSummary(data) {
  const wdl = data.asset_wdl;
  if (!wdl) return;
  let totalW=0, totalD=0, totalL=0, totalPnl=0;
  const assetHtml = [];
  for (const [asset, stats] of Object.entries(wdl)) {
    totalW += stats.wins; totalD += stats.draws; totalL += stats.losses;
    totalPnl += stats.total_pnl;
    const color = ASSET_COLORS[asset] || '#999';
    const pnlClass = stats.total_pnl >= 0 ? 'pos' : 'neg';
    // Sparkline from pnl_history
    let sparkHtml = '';
    if (stats.pnl_history && stats.pnl_history.length > 0) {
      const maxAbs = Math.max(...stats.pnl_history.map(v=>Math.abs(v)), 0.01);
      sparkHtml = '<div class="ps-sparkline">' + stats.pnl_history.slice(-20).map(v => {
        const h = Math.max(1, Math.abs(v) / maxAbs * 18);
        return `<div class="bar ${v>=0?'pos':'neg'}" style="height:${h}px"></div>`;
      }).join('') + '</div>';
    }
    assetHtml.push(`<div class="ps-asset-row">
      <div class="ps-asset-icon" style="background:${color}22;color:${color}">${(ASSET_ICONS[asset]||asset[0]).toUpperCase()}</div>
      <div class="ps-asset-info">
        <div class="ps-asset-name">${asset.toUpperCase()}</div>
        <div class="ps-asset-wdl"><span style="color:var(--green)">${stats.wins}W</span> / ${stats.draws}D / <span style="color:var(--red)">${stats.losses}L</span> &mdash; ${stats.total} markets</div>
      </div>
      ${sparkHtml}
      <div class="ps-asset-pnl ${pnlClass}">${stats.total_pnl>=0?'+':''}$${stats.total_pnl.toFixed(2)}</div>
    </div>`);
  }
  const totalGames = totalW + totalD + totalL;
  const winRate = totalGames > 0 ? ((totalW / totalGames) * 100).toFixed(0) + '%' : '--';
  document.getElementById('ps-total').textContent = (totalPnl>=0?'+':'') + '$' + totalPnl.toFixed(2);
  document.getElementById('ps-total').className = 'ps-val ' + (totalPnl >= 0 ? 'pos' : 'neg');
  document.getElementById('ps-winrate').textContent = winRate;
  document.getElementById('ps-wdl').textContent = `${totalW}/${totalD}/${totalL}`;
  document.getElementById('ps-assets').innerHTML = assetHtml.join('');
  // Update topbar
  document.getElementById('tb-winrate').textContent = winRate;
  document.getElementById('tb-wdl').textContent = `${totalW}/${totalD}/${totalL}`;
}

// ─── Bot node PnL + market chips ─────────────────────────────────────────────
let pnlHistory = []; // running total PnL snapshots for chart

function updateBotNodes(data) {
  const botNodes = nodes.filter(n=>n.type==='bot');
  if (!botNodes.length) return;
  const wdl = data.asset_wdl || {};
  const trueBalance = data.true_balance ?? latestData?.true_balance;
  const startBalance = data.starting_balance ?? latestData?.starting_balance;

  for (const bot of botNodes) {
    // Aggregate WDL from all connected markets
    const connectedMarkets = connections.filter(c=>c.from===bot.id).map(c=>nodes.find(n=>n.id===c.to)).filter(Boolean);
    let bW=0, bD=0, bL=0, bPnl=0, bTrades=0;
    for (const mkt of connectedMarkets) {
      const assetWdl = wdl[mkt.asset];
      if (assetWdl) { bW+=assetWdl.wins; bD+=assetWdl.draws; bL+=assetWdl.losses; bPnl+=assetWdl.total_pnl; }
      // Count active trades
      if (data.active_markets) {
        for (const [slug, d] of Object.entries(data.active_markets)) {
          if (d.asset === mkt.asset) bTrades += d.paper_trader?.trade_count || 0;
        }
      }
    }
    // If no connected markets, show total
    if (!connectedMarkets.length && trueBalance !== undefined && startBalance !== undefined) {
      bPnl = trueBalance - startBalance;
      for (const [a, s] of Object.entries(wdl)) { bW+=s.wins; bD+=s.draws; bL+=s.losses; }
      if (data.active_markets) {
        for (const [slug, d] of Object.entries(data.active_markets)) {
          bTrades += d.paper_trader?.trade_count || 0;
        }
      }
    }
    const pnlEl = document.getElementById(`bp-pnl-${bot.id}`);
    if (pnlEl) {
      pnlEl.textContent = (bPnl>=0?'+':'') + '$' + bPnl.toFixed(2);
      pnlEl.className = 'bs-val ' + (bPnl >= 0 ? 'pos' : 'neg');
    }
    const wdlEl = document.getElementById(`bp-wdl-${bot.id}`);
    if (wdlEl) wdlEl.textContent = `${bW}/${bD}/${bL}`;
    const trEl = document.getElementById(`bp-trades-${bot.id}`);
    if (trEl) trEl.textContent = bTrades;

    // Market chips
    const chipsEl = document.getElementById(`bp-chips-${bot.id}`);
    if (chipsEl) {
      chipsEl.innerHTML = connectedMarkets.map(m => {
        const c = ASSET_COLORS[m.asset]||'#999';
        return `<div class="bot-market-chip" style="border-color:${c}44;color:${c}" onclick="event.stopPropagation();disconnectMarket(${bot.id},${m.id})">
          ${m.asset.toUpperCase()} <span class="chip-x">&times;</span>
        </div>`;
      }).join('');
    }

    // PnL mini chart (resolved markets only)
    drawBotPnlChart(bot.id);
  }
}

function getBotPnlHistory(botId) {
  // Only use resolved market results from history — not live ticks
  const wdl = latestData?.asset_wdl || {};
  const connectedMarkets = connections.filter(c=>c.from===botId).map(c=>nodes.find(n=>n.id===c.to)).filter(Boolean);
  if (connectedMarkets.length === 0) {
    // Show all assets combined
    let combined = [];
    for (const [a, s] of Object.entries(wdl)) {
      if (s.pnl_history) combined = combined.concat(s.pnl_history);
    }
    // Convert to cumulative
    let cum = 0;
    return combined.map(v => (cum += v, cum));
  }
  // Merge pnl_history from connected assets, sorted by time (interleaved)
  let merged = [];
  for (const mkt of connectedMarkets) {
    const s = wdl[mkt.asset];
    if (s?.pnl_history) merged = merged.concat(s.pnl_history);
  }
  let cum = 0;
  return merged.map(v => (cum += v, cum));
}
function drawBotPnlChart(botId) {
  const arr = getBotPnlHistory(botId);
  if (!arr.length) arr.push(0); // show zero line

  const canvas = document.getElementById(`bp-chart-${botId}`);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  const w = rect.width, h = rect.height;
  canvas.width = w * dpr; canvas.height = h * dpr;
  canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (arr.length < 2) return;
  const minV = Math.min(...arr, 0);
  const maxV = Math.max(...arr, 0);
  const range = maxV - minV || 1;
  const pad = 2;

  // Zero line
  const zeroY = h - pad - ((0 - minV) / range) * (h - pad * 2);
  ctx.strokeStyle = 'rgba(107,109,128,.2)';
  ctx.lineWidth = 1;
  ctx.setLineDash([3,3]);
  ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(w, zeroY); ctx.stroke();
  ctx.setLineDash([]);

  // Line
  const isPos = currentPnl >= 0;
  ctx.strokeStyle = isPos ? '#00d68f' : '#ff4d6a';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  arr.forEach((v, i) => {
    const x = (i / (arr.length - 1)) * w;
    const y = h - pad - ((v - minV) / range) * (h - pad * 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill gradient
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, isPos ? 'rgba(0,214,143,.15)' : 'rgba(255,77,106,.15)');
  grad.addColorStop(1, 'transparent');
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
}

function addMarketToBot(botId) {
  // Open market modal, linking results to this bot
  ctxPos = {x: innerWidth/2, y: innerHeight/2};
  addMarket();
  // After markets are added, auto-connect them to this bot
  window._pendingBotLink = botId;
}
function disconnectMarket(botId, marketId) {
  connections = connections.filter(c=>!(c.from===botId && c.to===marketId));
  drawConnections();
  updateBotNodes(latestData || {});
}

// Patch confirmMarkets to auto-link if pending
const _origConfirmMarkets = confirmMarkets;
confirmMarkets = function() {
  const before = nodes.filter(n=>n.type==='market').map(n=>n.id);
  _origConfirmMarkets();
  if (window._pendingBotLink) {
    const after = nodes.filter(n=>n.type==='market').map(n=>n.id);
    const newIds = after.filter(id=>!before.includes(id));
    for (const mid of newIds) {
      if (!connections.find(c=>c.from===window._pendingBotLink && c.to===mid)) {
        connections.push({from: window._pendingBotLink, to: mid});
      }
    }
    window._pendingBotLink = null;
    drawConnections();
  }
};

// ─── WebSocket ───────────────────────────────────────────────────────────────
function connect() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(protocol + '//' + location.host + '/ws');
  ws.onopen = () => {
    const dot = document.getElementById('conn-dot');
    dot.classList.add('on'); dot.title = 'Connected';
    // If live mode, ensure bot is armed and running after (re)connect
    if (isLiveMode()) {
      setTimeout(() => {
        ws.send(JSON.stringify({action: 'arm_live'}));
        ws.send(JSON.stringify({action: 'start_current_market'}));
        ensureRunningDelayed();
      }, 300);
    }
  };
  ws.onclose = () => {
    const dot = document.getElementById('conn-dot');
    dot.classList.remove('on'); dot.title = 'Disconnected';
    setTimeout(connect, 2000);
  };
  ws.onerror = () => {};
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    latestData = {...(latestData||{}), ...data};
    updateUI(data);
  };
}

function updateUI(data) {
  // Top bar stats
  if (data.true_balance !== undefined && data.starting_balance !== undefined) {
    const pnl = data.true_balance - data.starting_balance;
    const pnlEl = document.getElementById('tb-pnl');
    pnlEl.textContent = (pnl>=0?'+':'') + '$' + pnl.toFixed(2);
    pnlEl.className = 'tv ' + (pnl >= 0 ? 'pos' : 'neg');
    document.getElementById('tb-balance').textContent = '$' + data.true_balance.toFixed(2);
    // Also show live cost/proceeds if available
    if (data.is_live) {
      const balEl = document.getElementById('tb-balance');
      if (balEl) balEl.title = `Spent: $${(data.total_locked_profit||0).toFixed(2)} locked`;
    }
  }
  if (data.active_markets) {
    document.getElementById('tb-markets').textContent = Object.keys(data.active_markets).length;
  }
  if (data.is_live !== undefined) {
    document.getElementById('mode-paper').classList.toggle('active', !data.is_live);
    document.getElementById('mode-live').classList.toggle('active', data.is_live);
  }

  // Update pause state
  if (data.paused !== undefined) {
    // Update all market play buttons
    nodes.filter(n=>n.type==='market').forEach(n => {
      const btn = document.getElementById(`mp-play-${n.id}`);
      if (!btn) return;
      if (data.paused) {
        btn.className = 'play-btn start';
        btn.innerHTML = '&#9654; Start';
      } else {
        btn.className = 'play-btn running';
        btn.innerHTML = '&#9646;&#9646; Running';
      }
    });
  }

  // Update market node cards
  if (data.active_markets) {
    for (const [slug, mData] of Object.entries(data.active_markets)) {
      const mNode = nodes.find(n=>n.type==='market'&&n.asset===mData.asset);
      if (!mNode) continue;
      const pt = mData.paper_trader || {};
      const upEl = document.getElementById(`mp-up-${mNode.id}`);
      const dnEl = document.getElementById(`mp-dn-${mNode.id}`);
      if (upEl) upEl.textContent = mData.up_price ? '$' + mData.up_price.toFixed(3) : '--';
      if (dnEl) dnEl.textContent = mData.down_price ? '$' + mData.down_price.toFixed(3) : '--';
      const timeEl = document.getElementById(`mp-time-${mNode.id}`);
      if (timeEl) timeEl.textContent = mData.window_time || '--:--';
      // Shares — use live inventory if available
      const sharesEl = document.getElementById(`mp-shares-${mNode.id}`);
      if (sharesEl) {
        const qu = pt.is_live ? (pt.live_qty_up || 0) : (pt.qty_up || 0);
        const qd = pt.is_live ? (pt.live_qty_down || 0) : (pt.qty_down || 0);
        if (qu > 0.1 || qd > 0.1) {
          sharesEl.innerHTML = `<span style="color:var(--green)">${qu.toFixed(1)}U</span> <span style="color:var(--red)">${qd.toFixed(1)}D</span>`;
        } else {
          sharesEl.textContent = '--';
        }
      }
      // PnL — use live PnL if available, otherwise paper
      const pnlEl = document.getElementById(`mp-pnl-${mNode.id}`);
      if (pnlEl) {
        const rp = pt.is_live && pt.live_pnl_realtime !== undefined
          ? pt.live_pnl_realtime
          : (pt.realised_pnl || pt.locked_profit || 0);
        pnlEl.textContent = (rp >= 0 ? '+' : '') + '$' + rp.toFixed(2);
        pnlEl.style.color = rp >= 0 ? 'var(--green)' : 'var(--red)';
      }
      // Worst / Best
      const wbEl = document.getElementById(`mp-wb-${mNode.id}`);
      if (wbEl) {
        const pUp = pt.pnl_if_up_wins, pDn = pt.pnl_if_down_wins;
        if (pUp !== undefined && pDn !== undefined) {
          const worst = Math.min(pUp, pDn), best = Math.max(pUp, pDn);
          wbEl.innerHTML = `<span style="color:var(--red)">${worst >= 0 ? '+' : ''}$${worst.toFixed(2)}</span> / <span style="color:var(--green)">${best >= 0 ? '+' : ''}$${best.toFixed(2)}</span>`;
        }
      }
      const tradesEl = document.getElementById(`mp-trades-${mNode.id}`);
      if (tradesEl) tradesEl.textContent = pt.trade_count || 0;
      const badge = document.getElementById(`mkt-badge-${mNode.id}`);
      if (badge) {
        if (pt.market_status === 'resolved') {
          badge.textContent = 'RESOLVED'; badge.style.background = 'rgba(77,142,255,.12)'; badge.style.color = 'var(--blue)';
        } else if ((pt.trade_count||0) > 0) {
          badge.textContent = 'TRADING'; badge.style.background = 'rgba(0,214,143,.12)'; badge.style.color = 'var(--green)';
        } else {
          badge.textContent = 'ACTIVE'; badge.style.background = 'rgba(255,193,69,.12)'; badge.style.color = 'var(--yellow)';
        }
      }
    }
  }

  // Activity log
  if (data.trade_log) updateActivityLog(data);

  // PnL summary
  if (data.asset_wdl) updatePnlSummary(data);

  // Bot node stats
  updateBotNodes(data);

  // Update detail panel if open
  updateDetailPanel();
}

// ─── Workspace persistence ───────────────────────────────────────────────────
let _savePending = false;
function saveWorkspace() {
  if (_savePending) return;
  _savePending = true;
  setTimeout(() => {
    _savePending = false;
    const state = {
      nodes: nodes.map(n => ({id:n.id, type:n.type, name:n.name, strategy:n.strategy, asset:n.asset, x:n.x, y:n.y})),
      connections: connections.map(c => ({from:c.from, to:c.to})),
      nextId
    };
    fetch('/api/workspace', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(state)
    }).catch(()=>{});
  }, 500); // debounce 500ms
}

async function loadWorkspace() {
  try {
    const r = await fetch('/api/workspace');
    const state = await r.json();
    if (!state.nodes || !state.nodes.length) return false;
    nextId = state.nextId || 1;
    for (const n of state.nodes) {
      const node = {id:n.id, type:n.type, name:n.name, strategy:n.strategy, asset:n.asset, x:n.x, y:n.y};
      nodes.push(node);
      createNodeEl(node);
      if (node.id >= nextId) nextId = node.id + 1;
    }
    connections = (state.connections || []).map(c => ({from:c.from, to:c.to}));
    drawConnections();
    return true;
  } catch(e) {
    console.warn('loadWorkspace failed', e);
    return false;
  }
}

// Hook save into all mutation functions
const _origCreateNodeEl = createNodeEl;
createNodeEl = function(node) { _origCreateNodeEl(node); saveWorkspace(); };
const _origRemoveSelected = removeSelected;
removeSelected = function() { _origRemoveSelected(); saveWorkspace(); };
// Save after connection changes
const _origFinishConnect = finishConnect;
finishConnect = function(e) { _origFinishConnect(e); saveWorkspace(); };
const _origDisconnectMarket = disconnectMarket;
disconnectMarket = function(botId, marketId) { _origDisconnectMarket(botId, marketId); saveWorkspace(); };
// Save after drag ends
const _origMouseup = document.onmouseup;
document.addEventListener('mouseup', () => { if (dragging) setTimeout(saveWorkspace, 100); });
document.addEventListener('touchend', () => { if (dragging) setTimeout(saveWorkspace, 100); });

// ─── Init ────────────────────────────────────────────────────────────────────
connect();

// Prevent workspace right-click bubbling
document.addEventListener('contextmenu', e => {
  if (!e.target.closest('#workspace')) e.preventDefault();
});

// Click workspace to deselect (only if we didn't pan)
document.getElementById('workspace').addEventListener('click', e => {
  if (e.target.closest('.node') || e.target.closest('#zoom-indicator')) return;
  selectedNode = null;
  document.querySelectorAll('.node').forEach(n=>n.classList.remove('selected'));
});

// Load workspace from server, then settings
(async () => {
  // Load saved workspace layout
  const loaded = await loadWorkspace();

  // Load settings for mode
  try {
    const s = await fetch('/api/settings').then(r=>r.json());
    if (s.live_trading) {
      document.getElementById('mode-paper').classList.remove('active');
      document.getElementById('mode-live').classList.add('active');
    }
    // If no saved workspace, auto-create market nodes from active assets
    if (!loaded) {
      const assets = s.active_assets || [];
      let wx = 300, wy = 80;
      for (const a of assets) {
        if (nodes.find(n=>n.type==='market'&&n.asset===a)) continue;
        const node = {id:nextId++, type:'market', asset:a, x:wx, y:wy};
        nodes.push(node);
        createNodeEl(node);
        wx += 260;
      }
    }
  } catch(e) {}
})();
</script>
</body>
</html>

"""


class PaperTrader:
    """Gabagool v7 paper trading bot - RECOVERY MODE ENABLED"""
    
    def __init__(self, cash_ref: dict, market_slug: str, market_budget: float):
        """
        cash_ref: A dict with 'balance' key that's shared across all traders
        market_slug: The market this trader is for
        """
        self.cash_ref = cash_ref  # Shared cash balance
        self.market_slug = market_slug
        self.qty_up = 0.0
        self.qty_down = 0.0
        self.cost_up = 0.0
        self.cost_down = 0.0
        self.trade_log = []
        self.trade_count = 0
        self.market_status = 'open'
        self.resolution_outcome = None
        self.final_pnl = None
        self.final_pnl_gross = None
        self.payout = 0.0
        self.last_fees_paid = 0.0
        self.market_budget = market_budget
        self.starting_balance = market_budget

        # Prefer "paired" compounding once profit is locked:
        # Buying equal UP+DOWN at a favorable combined price increases locked profit
        # while keeping worst-case protected.
        # v12: More aggressive compounding
        self.pair_growth_max_pair_price = 0.99   # Only compound when (up_price + down_price) <= this (WAS 0.98)
        self.pair_growth_budget_fraction = 0.70  # Use up to 70% of remaining budget per compound attempt (WAS 0.50)
        self.pair_growth_min_improvement = 0.005 # Require at least $0.005 improvement (WAS 0.01)
        self.growth_min_locked_after_trade = 0.00  # One-sided growth must keep locked profit >= 0
        
        # === TRADING MODE TRACKING ===
        self.current_mode = 'idle'  # idle, entry, hedge, priority_fix, improve, rebalance, optimize
        self.mode_reason = ''
        
        # === GABAGOOL v10 - POSITION IMPROVEMENT STRATEGY ===
        # Core principle: Continuously improve position to make hedging easier
        # If we buy UP @ $0.46, and later UP is $0.38, buy more to lower average!
        # This widens the profitable hedge window.
        
        # Trading strategy parameters
        self.cheap_threshold = 0.45      # What we consider "cheap" for entry (WAS 0.48)
        self.very_cheap_threshold = 0.40 # Very cheap - accumulate more
        self.force_balance_threshold = 0.52  # Max price to pay when balancing (WAS 0.55)
        self.max_balance_price = 0.65    # Absolute max for emergency balance
        self.target_pair_cost = 0.93     # Ideal pair cost target (WAS 0.95)
        self.max_pair_cost = 0.98        # CRITICAL: Never buy if this would push pair over (WAS 0.995)
        
        # === POSITION IMPROVEMENT PARAMETERS ===
        # Key insight: Buying more at lower price LOWERS the average!
        # Example: avg_UP=$0.46, buy more @$0.38 → new avg ~$0.42
        # Now DOWN only needs to be <$0.58 instead of <$0.54!
        self.improvement_threshold = 0.005   # Buy more if price is 0.5 cents below average (was 0.001)
        self.min_improvement_pct = 0.01      # Or 1% below average (was 0.005)
        self.force_improve_pct = 0.05        # Force average-down if price drops 5%+ vs avg
        self.max_imbalance_for_improvement = 3.0  # Max qty ratio during improvement phase
        self.improvement_trade_pct = 0.01     # Use 1% of budget per improvement (was 0.005)
        
        # Position sizing - scaled to bankroll
        self.min_trade_size = 1.00       # Larger min trade to reduce fees (was $0.10)
        self.max_single_trade = 25.0     # Cap at $25 per trade (was $15)
        self.cooldown_seconds = 8        # Slow down: 8 seconds between trades (was 3) - fewer but bigger trades
        self.last_trade_time = 0
        self.first_trade_time = 0
        self.initial_trade_usd = 5.0     # Start with $5 (was $3) - larger initial trades
        self.max_position_pct = 0.85     # Use max 85% of budget (keep 15% reserve)
        self.force_balance_after_seconds = 120
        
        # === TIME-BASED BALANCE ENFORCEMENT ===
        # First 30 seconds: allow imbalance to take good entry prices
        # After 30 seconds: actively minimize delta % - prioritize smaller side
        self.balance_enforcement_delay = 30  # Grace period for initial positioning
        
        # === LOSS PROTECTION ===
        # Bot will continuously try to improve positions - only abandon if mathematically impossible
        self.abandon_threshold_pair_cost = 1.02  # If pair > $1.02, stop trying (mathematically unprofitable)
        self.conservative_mode_loss_threshold = -5.0  # Go conservative at -$5
        
        # === SPREAD-AWARE TRADING ===
        # Key insight: High spread = good opportunity to buy cheap side
        # BUT: Don't over-favor cheap side - we need balance on BOTH sides!
        self.high_spread_threshold = 0.35  # Was 0.25, now 0.35 (harder to trigger)
        self.medium_spread_threshold = 0.25  # Was 0.15, now 0.25
        self.spread_multiplier = 1.1  # Was 1.2, now only 1.1x boost (even less aggressive)
        
        # === STRATEGIC IMBALANCE (Asymmetric PnL) ===
        # Don't always aim for perfect 1:1 - accept imbalance if it gives better averages
        self.strategic_imbalance_max = 1.3  # Allow 30% more on the cheaper-average side
        self.prefer_better_average = True  # Prefer more qty on side with better average
        
        # === PROFIT GROWTH MODE ===
        # v12: AGGRESSIVE profit growth - never stop trading while market is open!
        # After securing locked profit, continue buying to maximize upside
        self.enable_profit_growth = True
        self.min_locked_for_growth = 0.001  # Almost any profit enables growth (WAS 0.01)
        self.min_target_locked_profit = 1.0  # Lower target: $1 locked is good start (WAS 3.0)
        self.growth_budget_pct = 0.70      # Use up to 70% of budget for growth trades (WAS 0.60)
        self.growth_max_pair_cost = 0.99   # Allow growth up to 0.99 pair (WAS 0.98)
        self.growth_max_pair_cost_low_profit = 0.998  # Be very aggressive when building profit (WAS 0.995)
        self.growth_favor_probability = True  # Favor side with higher market probability
        self.growth_favor_better_avg = True  # Favor side with better average
        self.growth_max_single_trade = 40.0  # Allow larger trades in growth mode (WAS 30.0)
        
        # === GUARANTEED PROFIT PARAMETERS ===
        # NEW STRATEGY: Ensure min(qty_up, qty_down) > total_spent
        # This guarantees profit regardless of outcome!
        # Position Delta % = |UP - DOWN| / (UP + DOWN) × 100
        # v11: ULTRA STRICT BALANCE - Losses are from imbalance!
        self.ideal_balance_delta_pct = 2.0   # IDEAL: Keep position delta ≤ 2% (WAS 5%)
        self.max_flex_delta_pct = 5.0        # MAX FLEX: Allow up to 5% temporarily (WAS 15%)
        self.critical_ratio = 1.20           # CRITICAL: Stop buying larger side at 1.2x imbalance (WAS 2.0)
        self.emergency_ratio = 1.35          # EMERGENCY: Absolute hard stop at 1.35x (WAS 2.5)
        self.emergency_hedge_ratio = 1.50    # Force emergency hedge even at pair 1.05 when ratio > 1.5x (WAS 3.0)
        self.max_qty_ratio = 1.10            # Allow only 10% strategic imbalance (WAS 30%)
        
        # === FEE AWARENESS ===
        # Polymarket uses dynamic fees: highest at $0.50 (1.56%), lowest at extremes
        # Fee formula: fee_rate ≈ price * (1 - price) * 0.0624 (capped at ~1.56%)
        # CRITICAL: For guaranteed profit, pair_cost MUST be < $1.00
        # With ~1.5% avg fees, we need pair_cost < ~$0.985 to profit
        self.max_entry_pair_potential = 0.98  # STRICT: Only enter if pair < $0.98

        # === PROFIT GROWTH MODE ===
        # Allow continued buying after locked profit is secured, but only if it
        # improves locked profit and keeps pair_cost under target.
        self.allow_profit_growth = True
        self.min_locked_profit_increase = 0.01  # Only 1 cent improvement needed

        # === BANKROLL RESERVES ===
        self.pre_hedge_reserve_ratio = 0.10   # Keep 10% of budget before hedging
        self.post_hedge_reserve_ratio = 0.05  # Keep 5% once both sides exist
        self.min_reserve_cash = 5.0           # Always keep at least $5 available
        self.reserve_price_floor = 0.05       # Minimal assumed hedge price

        # === GUARANTEED BREAK-EVEN SYSTEM ===
        # CRITICAL: Always keep enough cash to hedge entire position to break-even
        # Formula: max_spend = budget * min_expected_avg_price
        # With avg floor ~$0.15: max_spend = $200 * 0.15 = $30
        # INCREASED: Was $35, now $50 to allow better positioning
        self.max_spend_per_side = 50.0        # Hard limit per side ($50 of $200)
        self.breakeven_hedge_price = 0.90     # Worst case hedge price assumption
        self.enable_breakeven_check = True    # Enable break-even reserve check
        
        # === BREAK-EVEN HEDGE TRIGGERS ===
        # When to accept pair <= 1.00 instead of waiting for pair < 0.99
        self.breakeven_time_threshold = 180   # Accept break-even when < 3 min to close
        self.breakeven_price_threshold = 0.92 # Accept break-even when opposite side > $0.92
        self.max_acceptable_pair_profit = 0.99  # Normal: require profit (pair < 0.99)
        self.max_acceptable_pair_breakeven = 1.00  # Fallback: accept break-even (pair <= 1.00)
        
        # === STOP BUYING GUARD ===
        # CRITICAL: Stop buying when opposite side is too expensive for ANY hedge
        # If opposite > this, even break-even is impossible with reasonable avg
        self.stop_buying_opposite_price = 0.85  # Stop if opposite > $0.85
        
        # === ACCELERATED LADDER ===
        # Buy more when price is low to drag down average faster
        # Larger amounts to minimize number of trades (reduce fees)
        self.ladder_tiers = [
            # (price_threshold, spend_amount)
            (0.10, 6.0),    # Below $0.10: spend $6 per rung (was $4) - fewer trades
            (0.20, 4.5),    # $0.10 - $0.20: spend $4.5 per rung (was $3)
            (0.30, 3.0),    # $0.20 - $0.30: spend $3 per rung (was $2.5)
            (1.00, 2.0),    # Above $0.30: spend $2 per rung (was $1.5)
        ]

        # === IMPROVEMENT THROTTLE ===
        self.improvement_spend_window = 2.0   # Seconds to look back when throttling
        self.improvement_spend_cap = 15.0     # Max spend allowed per window on improvements
        self.improvement_spend_log = {
            'UP': deque(),
            'DOWN': deque()
        }
        self.improvement_step_price = 0.02   # Require $0.02 drop before next ladder fill
        self.last_improvement_price = {
            'UP': None,
            'DOWN': None
        }
    
    @staticmethod
    def calculate_fee(price: float, qty: float) -> float:
        """
        Calculate Polymarket fee based on price.
        Fee is highest at $0.50 (~1.56%) and approaches 0 at extremes ($0.01, $0.99)
        
        Fee table (per 100 shares):
        $0.50 → $0.78 (1.56%)
        $0.45 → $0.69 (1.53%)  
        $0.40 → $0.58 (1.44%)
        $0.30 → $0.33 (1.10%)
        $0.20 → $0.13 (0.64%)
        $0.10 → $0.02 (0.20%)
        $0.05 → $0.003 (0.06%)
        """
        # Effective rate lookup table (interpolated)
        fee_table = {
            0.01: 0.0000, 0.05: 0.0006, 0.10: 0.0020, 0.15: 0.0041,
            0.20: 0.0064, 0.25: 0.0088, 0.30: 0.0110, 0.35: 0.0129,
            0.40: 0.0144, 0.45: 0.0153, 0.50: 0.0156, 0.55: 0.0153,
            0.60: 0.0144, 0.65: 0.0129, 0.70: 0.0110, 0.75: 0.0088,
            0.80: 0.0064, 0.85: 0.0041, 0.90: 0.0020, 0.95: 0.0006,
            0.99: 0.0000
        }
        
        # Find closest prices in table and interpolate
        prices = sorted(fee_table.keys())
        
        if price <= prices[0]:
            rate = fee_table[prices[0]]
        elif price >= prices[-1]:
            rate = fee_table[prices[-1]]
        else:
            # Linear interpolation
            for i in range(len(prices) - 1):
                if prices[i] <= price <= prices[i + 1]:
                    p1, p2 = prices[i], prices[i + 1]
                    r1, r2 = fee_table[p1], fee_table[p2]
                    rate = r1 + (r2 - r1) * (price - p1) / (p2 - p1)
                    break
        
        trade_value = price * qty
        return trade_value * rate
    
    def calculate_total_fees(self) -> float:
        """Calculate total fees for current positions"""
        fee_up = self.calculate_fee(self.avg_up, self.qty_up) if self.qty_up > 0 else 0
        fee_down = self.calculate_fee(self.avg_down, self.qty_down) if self.qty_down > 0 else 0
        return fee_up + fee_down
    
    def remaining_budget(self) -> float:
        total_spent = self.cost_up + self.cost_down
        budget_limit = self.starting_balance * self.max_position_pct
        return max(0.0, budget_limit - total_spent)
    
    def affordable_cash(self, fraction: float = 1.0) -> float:
        fraction = max(0.0, min(1.0, fraction))
        return max(0.0, min(self.cash * fraction, self.remaining_budget()))
    
    def capped_spend(self, desired_spend: float, fraction: float = 1.0) -> float:
        return min(desired_spend, self.affordable_cash(fraction))

    def _prune_improvement_window(self, side: str, now: Optional[float] = None):
        now = now if now is not None else time.time()
        log = self.improvement_spend_log.get(side)
        if log is None:
            return
        window = self.improvement_spend_window
        while log and now - log[0][0] > window:
            log.popleft()

    def _recent_improvement_spend(self, side: str, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        self._prune_improvement_window(side, now)
        log = self.improvement_spend_log.get(side)
        if not log:
            return 0.0
        return sum(amount for _, amount in log)

    def _check_breakeven_reserve(self, side: str, price: float, my_qty: float, my_cost: float, desired_spend: float) -> tuple:
        """
        Check if we have enough cash to hedge the entire position to break-even after this purchase.
        
        For break-even: pair_cost = 1.00, so hedge_price = 1 - avg_price
        Hedge cost = qty * hedge_price
        
        We must have: remaining_cash >= hedge_cost after the purchase
        
        Returns: (ok, allowed_spend, reason)
        """
        new_qty = my_qty + (desired_spend / price)
        new_cost = my_cost + desired_spend
        new_avg = new_cost / new_qty if new_qty > 0 else price
        
        # Worst case hedge price: use breakeven_hedge_price as ceiling
        breakeven_hedge_price = min(1.0 - new_avg, self.breakeven_hedge_price)
        hedge_cost = new_qty * breakeven_hedge_price
        
        cash_after = self.cash - desired_spend
        
        if cash_after < hedge_cost:
            # Calculate max spend that allows break-even hedge
            # cash - spend >= (my_qty + spend/price) * breakeven_hedge_price
            # cash - spend >= my_qty * bhp + spend * bhp / price
            # cash - my_qty * bhp >= spend + spend * bhp / price
            # cash - my_qty * bhp >= spend * (1 + bhp / price)
            # spend <= (cash - my_qty * bhp) / (1 + bhp / price)
            current_hedge_cost = my_qty * breakeven_hedge_price
            available_for_spend = self.cash - current_hedge_cost
            max_spend = available_for_spend / (1 + breakeven_hedge_price / price)
            
            if max_spend < self.min_trade_size:
                return False, 0, f"Break-even reserve: need ${hedge_cost:.2f} for hedge, only ${cash_after:.2f} available"
            return True, max_spend, f"Capped to ${max_spend:.2f} for break-even reserve"
        
        return True, desired_spend, ""

    def _evaluate_improvement_throttle(self, side: str, desired_spend: float) -> tuple:
        if desired_spend <= 0:
            return True, 0.0, ""
        now = time.time()
        recent_spend = self._recent_improvement_spend(side, now)
        remaining_allowance = max(0.0, self.improvement_spend_cap - recent_spend)
        if desired_spend <= remaining_allowance + 1e-6:
            return True, desired_spend, ""
        return False, remaining_allowance, (
            f"Throttle: ${recent_spend:.2f} used last {self.improvement_spend_window:.0f}s (cap ${self.improvement_spend_cap:.2f})"
        )

    def record_improvement_spend(self, side: str, spend: float):
        if spend <= 0:
            return
        now = time.time()
        log = self.improvement_spend_log.get(side)
        if log is None:
            return
        self._prune_improvement_window(side, now)
        log.append((now, spend))

    def cap_qty_to_reserve(
        self,
        side: str,
        price: float,
        desired_qty: float,
        opposing_price: Optional[float] = None,
        iterations: int = 20
    ) -> float:
        """Shrink qty until reserve_ok passes while staying within budget."""
        if price <= 0 or desired_qty <= 0:
            return 0.0

        ok, _ = self.reserve_ok(side, price, desired_qty, opposing_price)
        if ok:
            return desired_qty

        low = 0.0
        high = desired_qty

        for _ in range(iterations):
            mid = (low + high) / 2.0
            ok, _ = self.reserve_ok(side, price, mid, opposing_price)
            if ok:
                low = mid
            else:
                high = mid

        return low

    def capped_spend_until_ok(
        self,
        side: str,
        price: float,
        desired_spend: float,
        opposing_price: Optional[float] = None,
        fraction: float = 1.0,
        min_spend: float = 0.10,
        reduction_factor: float = 0.5,
        max_iter: int = 6
    ) -> float:
        """Try smaller spends until reserve_ok passes or min_spend reached."""
        spend = self.capped_spend(desired_spend, fraction)
        iteration = 0

        while spend >= min_spend and iteration < max_iter:
            qty = spend / price if price > 0 else 0.0
            if qty <= 0:
                break
            ok, _ = self.reserve_ok(side, price, qty, opposing_price)
            if ok:
                return spend
            spend *= reduction_factor
            iteration += 1

        return 0.0

    def cap_qty_to_reserve(
        self,
        side: str,
        price: float,
        desired_qty: float,
        opposing_price: Optional[float] = None,
        iterations: int = 20
    ) -> float:
        if price <= 0 or desired_qty <= 0:
            return 0.0
        ok, _ = self.reserve_ok(side, price, desired_qty, opposing_price)
        if ok:
            return desired_qty

        low = 0.0
        high = desired_qty
        for _ in range(iterations):
            mid = (low + high) / 2.0
            ok, _ = self.reserve_ok(side, price, mid, opposing_price)
            if ok:
                low = mid
            else:
                high = mid

        return low

    def _reserve_cash_needed_for_state(
        self,
        qty_up: float,
        qty_down: float,
        opposing_price: Optional[float] = None,
        cost_up: Optional[float] = None,
        cost_down: Optional[float] = None
    ) -> float:
        opposing_price = opposing_price if opposing_price is not None else 0.0
        cost_up = cost_up if cost_up is not None else self.cost_up
        cost_down = cost_down if cost_down is not None else self.cost_down

        if qty_up <= 0 and qty_down <= 0:
            return max(self.min_reserve_cash, self.market_budget * self.pre_hedge_reserve_ratio)

        if qty_up == 0 or qty_down == 0:
            qty_single = qty_up if qty_down == 0 else qty_down
            if qty_single <= 0:
                return max(self.min_reserve_cash, self.market_budget * self.pre_hedge_reserve_ratio)

            if qty_down == 0 and qty_up > 0:
                avg_single = cost_up / qty_up
            elif qty_up == 0 and qty_down > 0:
                avg_single = cost_down / qty_down
            else:
                avg_single = 0.0

            max_profitable_price = max(0.01, min(0.99, 0.99 - avg_single))
            observed_price = opposing_price if opposing_price > 0 else max_profitable_price
            est_price = max(self.reserve_price_floor, min(max_profitable_price, observed_price))

            dynamic = qty_single * est_price
            base = self.market_budget * self.pre_hedge_reserve_ratio
            return max(self.min_reserve_cash, base, dynamic)

        base = self.market_budget * self.post_hedge_reserve_ratio
        return max(self.min_reserve_cash, base)

    def reserve_ok(self, side: str, price: float, qty: float, opposing_price: Optional[float] = None) -> tuple:
        if price <= 0 or qty <= 0:
            return False, "Invalid trade sizing"
        cost = price * qty
        new_qty_up = self.qty_up + (qty if side == 'UP' else 0.0)
        new_qty_down = self.qty_down + (qty if side == 'DOWN' else 0.0)
        new_cost_up = self.cost_up + (cost if side == 'UP' else 0.0)
        new_cost_down = self.cost_down + (cost if side == 'DOWN' else 0.0)

        reserve_needed = self._reserve_cash_needed_for_state(
            new_qty_up,
            new_qty_down,
            opposing_price,
            new_cost_up,
            new_cost_down
        )
        budget_limit = self.market_budget * self.max_position_pct
        new_total_spent = new_cost_up + new_cost_down
        remaining_budget_after = budget_limit - new_total_spent
        cash_after = self.cash - cost

        if remaining_budget_after < -1e-6 or cash_after < -1e-6:
            return False, "Insufficient funds"

        if remaining_budget_after + 1e-6 < reserve_needed:
            return False, f"Need ${reserve_needed:.2f} budget reserved (have ${remaining_budget_after:.2f})"

        if cash_after + 1e-6 < reserve_needed:
            return False, f"Need ${reserve_needed:.2f} cash reserved (have ${cash_after:.2f})"

        return True, ""
        
    @property
    def cash(self):
        return self.cash_ref['balance']
    
    @cash.setter
    def cash(self, value):
        self.cash_ref['balance'] = value
        
    @property
    def avg_up(self) -> float:
        return self.cost_up / self.qty_up if self.qty_up > 0 else 0.0
    
    @property
    def avg_down(self) -> float:
        return self.cost_down / self.qty_down if self.qty_down > 0 else 0.0
    
    @property
    def pair_cost(self) -> float:
        if self.qty_up == 0 or self.qty_down == 0:
            return 0.0
        return self.avg_up + self.avg_down
    
    @property
    def locked_profit(self) -> float:
        """Guaranteed profit regardless of outcome (worst-case), accounting for fees"""
        min_qty = min(self.qty_up, self.qty_down)
        total_cost = self.cost_up + self.cost_down
        fees = self.calculate_total_fees()
        return min_qty - total_cost - fees
    
    @property
    def best_case_profit(self) -> float:
        """Best-case profit if the larger position wins"""
        max_qty = max(self.qty_up, self.qty_down)
        total_cost = self.cost_up + self.cost_down
        fees = self.calculate_total_fees()
        return max_qty - total_cost - fees
    
    @property
    def qty_ratio(self) -> float:
        """Ratio of larger qty to smaller qty (1.0 = perfectly balanced)"""
        if self.qty_up == 0 or self.qty_down == 0:
            return 0.0
        return max(self.qty_up, self.qty_down) / min(self.qty_up, self.qty_down)

    @property
    def position_delta_pct(self) -> float:
        """Position delta %: |UP - DOWN| / (UP + DOWN) × 100"""
        total = self.qty_up + self.qty_down
        if total == 0:
            return 0.0
        return abs(self.qty_up - self.qty_down) / total * 100

    def unrealized_pnl(self, up_price: float, down_price: float) -> float:
        total_cost = self.cost_up + self.cost_down
        current_value = (self.qty_up * up_price) + (self.qty_down * down_price)
        return current_value - total_cost

    def improves_pair_cost(self, side: str, price: float, qty: float) -> bool:
        if self.qty_up == 0 or self.qty_down == 0:
            return True
        _, new_pair_cost = self.simulate_buy(side, price, qty)
        return new_pair_cost < self.pair_cost

    def improves_locked_profit(self, side: str, price: float, qty: float) -> bool:
        return self.locked_profit_after_buy(side, price, qty) > self.locked_profit

    def locked_profit_after_buy(self, side: str, price: float, qty: float) -> float:
        """Calculate guaranteed profit after a hypothetical buy, with accurate fees"""
        cost = price * qty
        new_qty_up = self.qty_up + qty if side == 'UP' else self.qty_up
        new_qty_down = self.qty_down + qty if side == 'DOWN' else self.qty_down
        new_cost_up = self.cost_up + cost if side == 'UP' else self.cost_up
        new_cost_down = self.cost_down + cost if side == 'DOWN' else self.cost_down
        if new_qty_up == 0 or new_qty_down == 0:
            return 0.0
        
        # Calculate fees with new averages
        new_avg_up = new_cost_up / new_qty_up if new_qty_up > 0 else 0
        new_avg_down = new_cost_down / new_qty_down if new_qty_down > 0 else 0
        fee_up = self.calculate_fee(new_avg_up, new_qty_up)
        fee_down = self.calculate_fee(new_avg_down, new_qty_down)
        total_fees = fee_up + fee_down
        
        total_cost = new_cost_up + new_cost_down
        return min(new_qty_up, new_qty_down) - total_cost - total_fees

    def pair_cost_for_state(self, qty_up: float, cost_up: float, qty_down: float, cost_down: float) -> float:
        if qty_up <= 0 or qty_down <= 0:
            return float("inf")
        return (cost_up / qty_up) + (cost_down / qty_down)

    def best_pair_cost_after_spend(self, qty_up: float, cost_up: float, qty_down: float, cost_down: float,
                                   up_price: float, down_price: float, spend: float) -> float:
        best = self.pair_cost_for_state(qty_up, cost_up, qty_down, cost_down)
        if spend <= 0:
            return best

        for side, price in (("UP", up_price), ("DOWN", down_price)):
            if price <= 0:
                continue
            qty = spend / price
            if side == "UP":
                new_qty_up = qty_up + qty
                new_cost_up = cost_up + spend
                new_qty_down = qty_down
                new_cost_down = cost_down
            else:
                new_qty_down = qty_down + qty
                new_cost_down = cost_down + spend
                new_qty_up = qty_up
                new_cost_up = cost_up

            new_pair_cost = self.pair_cost_for_state(new_qty_up, new_cost_up, new_qty_down, new_cost_down)
            if new_pair_cost < best:
                best = new_pair_cost

        return best

    def can_recover_pair_cost(self, up_price: float, down_price: float, remaining_budget: float,
                              qty_up: Optional[float] = None, cost_up: Optional[float] = None,
                              qty_down: Optional[float] = None, cost_down: Optional[float] = None) -> bool:
        qty_up = self.qty_up if qty_up is None else qty_up
        cost_up = self.cost_up if cost_up is None else cost_up
        qty_down = self.qty_down if qty_down is None else qty_down
        cost_down = self.cost_down if cost_down is None else cost_down

        current_pair = self.pair_cost_for_state(qty_up, cost_up, qty_down, cost_down)
        if current_pair <= 1.0:
            return True
        if remaining_budget < self.min_trade_size:
            return False

    def _pair_reserve_ok(self, up_price: float, down_price: float, qty: float) -> tuple:
        """Reserve check for a paired buy of qty on BOTH sides."""
        if qty <= 0 or up_price <= 0 or down_price <= 0:
            return False, "Invalid paired sizing"

        cost_up = up_price * qty
        cost_down = down_price * qty
        total_cost = cost_up + cost_down

        new_qty_up = self.qty_up + qty
        new_qty_down = self.qty_down + qty
        new_cost_up = self.cost_up + cost_up
        new_cost_down = self.cost_down + cost_down

        reserve_needed = self._reserve_cash_needed_for_state(
            new_qty_up,
            new_qty_down,
            opposing_price=None,
            cost_up=new_cost_up,
            cost_down=new_cost_down,
        )

        budget_limit = self.market_budget * self.max_position_pct
        new_total_spent = new_cost_up + new_cost_down
        remaining_budget_after = budget_limit - new_total_spent
        cash_after = self.cash - total_cost

        if remaining_budget_after < -1e-6 or cash_after < -1e-6:
            return False, "Insufficient funds"

        if remaining_budget_after + 1e-6 < reserve_needed:
            return False, f"Need ${reserve_needed:.2f} budget reserved (have ${remaining_budget_after:.2f})"

        if cash_after + 1e-6 < reserve_needed:
            return False, f"Need ${reserve_needed:.2f} cash reserved (have ${cash_after:.2f})"

        return True, ""

    def _attempt_pair_profit_compound(
        self,
        up_price: float,
        down_price: float,
        locked_profit: float,
        pair_cost: float,
        remaining_budget: float,
        timestamp: str,
    ) -> List[tuple]:
        """Try to increase locked profit by buying equal qty of UP and DOWN."""
        trades: List[tuple] = []
        if up_price <= 0 or down_price <= 0:
            return trades

        combined = up_price + down_price
        if combined > self.pair_growth_max_pair_price + 1e-9:
            return trades

        growth_budget = min(
            remaining_budget * self.pair_growth_budget_fraction,
            self.growth_max_single_trade,
            self.affordable_cash(self.pair_growth_budget_fraction),
        )

        if growth_budget < self.min_trade_size * 2:
            return trades

        qty = growth_budget / combined
        if qty < 0.5:
            return trades

        # Simulate new averages and new locked profit (incl fees)
        new_qty_up = self.qty_up + qty
        new_qty_down = self.qty_down + qty
        new_cost_up = self.cost_up + (qty * up_price)
        new_cost_down = self.cost_down + (qty * down_price)
        new_avg_up = new_cost_up / new_qty_up
        new_avg_down = new_cost_down / new_qty_down
        new_pair_cost = new_avg_up + new_avg_down

        fee_up = self.calculate_fee(new_avg_up, new_qty_up)
        fee_down = self.calculate_fee(new_avg_down, new_qty_down)
        new_fees = fee_up + fee_down

        new_total_spent = new_cost_up + new_cost_down
        new_min_qty = min(new_qty_up, new_qty_down)
        new_locked = new_min_qty - new_total_spent - new_fees
        improvement = new_locked - locked_profit

        # Dynamic pair cost limit: be more aggressive when locked profit < $3
        max_allowed_pair = (
            self.growth_max_pair_cost_low_profit 
            if locked_profit < self.min_target_locked_profit 
            else self.growth_max_pair_cost
        )
        
        if new_pair_cost > max_allowed_pair + 1e-9:
            return trades

        if improvement < self.pair_growth_min_improvement:
            return trades

        ok, reason = self._pair_reserve_ok(up_price, down_price, qty)
        if not ok:
            print(f"⚠️  [PAIR GROWTH BLOCKED] {reason}")
            return trades

        self.current_mode = 'profit_growth'
        self.mode_reason = f'Compounding locked profit (+${improvement:.2f}) @ ${combined:.3f} pair'

        if self.execute_buy('UP', up_price, qty, timestamp):
            trades.append(('UP', up_price, qty))
        if self.execute_buy('DOWN', down_price, qty, timestamp):
            trades.append(('DOWN', down_price, qty))

        print(f"📈 [PAIR COMPOUND] Bought {qty:.1f} UP + {qty:.1f} DOWN | pair ${pair_cost:.3f}→${new_pair_cost:.3f} | locked ${locked_profit:.2f}→${new_locked:.2f}")
        return trades

        best = self.best_pair_cost_after_spend(
            qty_up,
            cost_up,
            qty_down,
            cost_down,
            up_price,
            down_price,
            remaining_budget
        )
        return best <= 1.0

    def evaluate_worst_positioned_side(self, up_price: float, down_price: float) -> tuple:
        """
        ENHANCED: Spread-aware + Asymmetric PnL optimization
        
        Evaluates which side to prioritize considering:
        - Spread opportunities (high spread = aggressive buying)
        - Expected value optimization (not just worst-case)
        - Strategic imbalance (allow more qty on better average side)
        - Discount opportunities
        - Pair cost trajectory
        
        Returns: (worst_side, severity_score, recommended_spend, reason)
        """
        if self.qty_up == 0 or self.qty_down == 0:
            return None, 0, 0, "Need both sides"
        
        # Calculate conservative mode status
        min_qty = min(self.qty_up, self.qty_down) if self.qty_up > 0 and self.qty_down > 0 else 0
        total_spent = self.cost_up + self.cost_down
        fees = self.calculate_total_fees()
        unrealized = min_qty - total_spent - fees
        in_conservative_mode = unrealized < self.conservative_mode_loss_threshold
        
        # === SPREAD ANALYSIS ===
        spread = abs(up_price - down_price)
        spread_pct = spread / max(up_price, down_price) if max(up_price, down_price) > 0 else 0
        high_spread = spread > self.high_spread_threshold
        medium_spread = spread > self.medium_spread_threshold
        
        # Calculate metrics for each side
        up_discount = self.avg_up - up_price  # Positive = good buying opportunity
        down_discount = self.avg_down - down_price
        
        up_discount_pct = up_discount / self.avg_up if self.avg_up > 0 else 0
        down_discount_pct = down_discount / self.avg_down if self.avg_down > 0 else 0
        
        # === EXPECTED VALUE CALCULATION ===
        # Use prices as probability estimates: up_price ≈ P(UP wins)
        prob_up = up_price
        prob_down = down_price
        pnl_if_up = self.qty_up - total_spent - fees
        pnl_if_down = self.qty_down - total_spent - fees
        expected_pnl = (prob_up * pnl_if_up) + (prob_down * pnl_if_down)
        worst_case_pnl = min(pnl_if_up, pnl_if_down)
        
        # Potential hedge cost for each side
        up_hedge_cost = self.qty_up * down_price
        down_hedge_cost = self.qty_down * up_price
        
        # Which side has worse avg vs current price?
        up_pair_if_buy = self.simulate_buy('UP', up_price, 10)[1]
        down_pair_if_buy = self.simulate_buy('DOWN', down_price, 10)[1]
        
        # Imbalance
        ratio = max(self.qty_up, self.qty_down) / min(self.qty_up, self.qty_down)
        up_is_lagging = self.qty_up < self.qty_down
        down_is_lagging = self.qty_down < self.qty_up
        
        # Score each side (higher = more urgent to fix)
        up_score = 0
        down_score = 0
        
        # CRITICAL: If locked profit is negative, prioritize LAGGING side heavily
        # Because only buying the lagging side increases min_qty!
        if unrealized < 0:
            if up_is_lagging:
                up_score += abs(unrealized) * 2  # Heavy weight based on loss size
            if down_is_lagging:
                down_score += abs(unrealized) * 2
        
        # === SPREAD BONUS: High spread = opportunity BUT NOT PRIMARY FOCUS ===
        # The cheaper side in high-spread situations is valuable
        # BUT: If pair cost is already good (<0.95), HEAVILY dampen this
        # CRITICAL: We need BOTH sides positioned well, not just the cheap side!
        current_pair = self.pair_cost
        spread_score_multiplier = 80  # Reduced from 150
        if current_pair < 0.95:
            spread_score_multiplier = 5  # Reduce from 30 to 5 when pair is good - almost ignore spread!
        
        if high_spread:
            if up_price < down_price:
                up_score += spread_pct * spread_score_multiplier
            else:
                down_score += spread_pct * spread_score_multiplier
        elif medium_spread:
            medium_spread_multiplier = 40  # Reduced from 80
            if current_pair < 0.95:
                medium_spread_multiplier = 3  # Reduce from 20 to 3 when pair is good
            if up_price < down_price:
                up_score += spread_pct * medium_spread_multiplier
            else:
                down_score += spread_pct * medium_spread_multiplier
        
        # Big discount = opportunity
        if up_discount_pct > 0.02:  # > 2% discount
            up_score += up_discount_pct * 100
        if down_discount_pct > 0.02:
            down_score += down_discount_pct * 100
        
        # === WORST CASE OPTIMIZATION (when pair is good) ===
        # If pair < 0.95 and worst case is negative, MASSIVELY prioritize fixing it
        # This is CRITICAL - if we lose on one side, fix that side!
        if current_pair < 0.95 and worst_case_pnl < 0:
            # Which side would improve worst case if we bought it?
            # If UP wins gives worst case, buy DOWN. If DOWN wins gives worst case, buy UP.
            if pnl_if_up < pnl_if_down:  # UP outcome is worse
                # Buying DOWN improves UP outcome (reduces avg_down, increases max acceptable up_price)
                # INCREASED: Was 3, now 10 - make this a TOP priority!
                down_score += abs(worst_case_pnl) * 10  # Massive bonus to fix worst case
            else:  # DOWN outcome is worse
                up_score += abs(worst_case_pnl) * 10  # Massive bonus to fix worst case
        
        # High avg = harder to hedge = more urgent to fix
        if self.avg_up > 0.55:
            up_score += (self.avg_up - 0.55) * 50
        if self.avg_down > 0.55:
            down_score += (self.avg_down - 0.55) * 50
        
        # STRATEGIC IMBALANCE: Allow more on better-average side
        # Don't penalize lagging if it has significantly better average
        better_avg_up = self.avg_up < self.avg_down - 0.05  # UP avg is 5¢ better
        better_avg_down = self.avg_down < self.avg_up - 0.05
        
        if up_is_lagging and ratio > 1.15:
            # Reduce penalty if UP has better average (we WANT more UP)
            penalty = (ratio - 1.0) * 10
            if better_avg_up and ratio < self.strategic_imbalance_max:
                penalty *= 0.3  # Reduce penalty by 70%
            up_score += penalty
        if down_is_lagging and ratio > 1.15:
            penalty = (ratio - 1.0) * 10
            if better_avg_down and ratio < self.strategic_imbalance_max:
                penalty *= 0.3
            down_score += penalty
        
        # Pair cost improvement potential
        current_pair = self.pair_cost
        if current_pair > 0.97:
            if up_pair_if_buy < current_pair:
                up_score += (current_pair - up_pair_if_buy) * 200
            if down_pair_if_buy < current_pair:
                down_score += (current_pair - down_pair_if_buy) * 200
        
        # === BLOCK TRADES THAT WORSEN WORST CASE (when pair is good) ===
        # If pair < 0.95 and worst case < 0, don't buy side that makes it worse
        if current_pair < 0.95 and worst_case_pnl < 0:
            # Buying UP makes "if DOWN wins" worse (more qty_up, same avg_down)
            # Buying DOWN makes "if UP wins" worse (more qty_down, same avg_up)
            if pnl_if_up < pnl_if_down:  # UP outcome is already worse
                # Don't make it worse by buying UP
                if up_score > down_score:
                    print(f"  🚫 [WORST CASE BLOCK] Refusing UP (would worsen worst case ${worst_case_pnl:.2f})")
                    up_score = 0  # Block UP
            else:  # DOWN outcome is worse
                if down_score > up_score:
                    print(f"  🚫 [WORST CASE BLOCK] Refusing DOWN (would worsen worst case ${worst_case_pnl:.2f})")
                    down_score = 0  # Block DOWN
        
        # === TIME-BASED BALANCE PRIORITY ===
        # After grace period, heavily boost priority for the smaller side
        time_since_first = time.time() - self.first_trade_time if self.first_trade_time > 0 else 0
        if time_since_first > self.balance_enforcement_delay:
            current_delta_pct = abs(self.qty_up - self.qty_down) / (self.qty_up + self.qty_down) * 100
            if current_delta_pct > self.ideal_balance_delta_pct:  # >5% imbalance
                balance_urgency = current_delta_pct * 5  # 10% delta = +50 severity
                if up_is_lagging:
                    up_score += balance_urgency
                    print(f"  ⏱️ [BALANCE BOOST] UP lagging - adding {balance_urgency:.1f} severity (delta {current_delta_pct:.1f}%)")
                if down_is_lagging:
                    down_score += balance_urgency
                    print(f"  ⏱️ [BALANCE BOOST] DOWN lagging - adding {balance_urgency:.1f} severity (delta {current_delta_pct:.1f}%)")
        
        # Decide worst side
        if up_score > down_score and up_score > 1.0:
            worst_side = 'UP'
            severity = up_score
            # Dynamic spend based on severity, discount, AND SPREAD
            base_spend_pct = 0.02  # Was 0.04
            if up_discount_pct > 0.10:  # >10% discount
                base_spend_pct = 0.10  # Was 0.20
            elif up_discount_pct > 0.05:  # >5% discount
                base_spend_pct = 0.06  # Was 0.125
            elif up_discount_pct > 0.02:  # >2% discount
                base_spend_pct = 0.04  # Was 0.075
            
            # SPREAD MULTIPLIER: Buy more during high spread
            if high_spread:
                base_spend_pct *= self.spread_multiplier  # 2x when spread is huge
            elif medium_spread:
                base_spend_pct *= 1.5
            
            recommended_spend = min(
                self.cash * base_spend_pct,
                self.max_single_trade,  # Respect single trade limit
                self.affordable_cash(base_spend_pct)
            )
            # Cut spending in half if we're in conservative mode (losing money)
            if in_conservative_mode:
                recommended_spend *= 0.5
            
            spread_info = f", spread={spread_pct*100:.0f}%" if spread > 0.10 else ""
            reason = f"UP: {up_discount_pct*100:.1f}% discount, avg=${self.avg_up:.3f}, score={up_score:.1f}{spread_info}"
        elif down_score > 1.0:
            worst_side = 'DOWN'
            severity = down_score
            base_spend_pct = 0.02  # Was 0.04
            if down_discount_pct > 0.10:
                base_spend_pct = 0.10  # Was 0.20
            elif down_discount_pct > 0.05:
                base_spend_pct = 0.06  # Was 0.125
            elif down_discount_pct > 0.02:
                base_spend_pct = 0.04  # Was 0.075
            
            # SPREAD MULTIPLIER
            if high_spread:
                base_spend_pct *= self.spread_multiplier
            elif medium_spread:
                base_spend_pct *= 1.5
            
            recommended_spend = min(
                self.cash * base_spend_pct,
                self.max_single_trade,
                self.affordable_cash(base_spend_pct)
            )
            if in_conservative_mode:
                recommended_spend *= 0.5
            
            spread_info = f", spread={spread_pct*100:.0f}%" if spread > 0.10 else ""
            reason = f"DOWN: {down_discount_pct*100:.1f}% discount, avg=${self.avg_down:.3f}, score={down_score:.1f}{spread_info}"
        else:
            return None, 0, 0, "No clear priority"
        
        return worst_side, severity, recommended_spend, reason
    
    def should_improve_position(self, side: str, price: float, opposing_price: float = None) -> tuple:
        """
        POSITION IMPROVEMENT STRATEGY
        
        Check if we should buy MORE of the same side to lower our average cost.
        This widens the profitable window for hedging the other side.
        
        Example:
        - Current: avg_DOWN = $0.51, max UP = $0.48 for profit
        - If DOWN drops to $0.45, buy more!
        - New avg_DOWN = $0.48, now max UP = $0.51 (easier to hit!)
        
        Returns: (should_buy, qty, reason)
        """
        my_qty = self.qty_up if side == 'UP' else self.qty_down
        my_cost = self.cost_up if side == 'UP' else self.cost_down
        my_avg = my_cost / my_qty if my_qty > 0 else 0
        other_qty = self.qty_down if side == 'UP' else self.qty_up
        other_cost = self.cost_down if side == 'UP' else self.cost_up
        other_avg = other_cost / other_qty if other_qty > 0 else 0
        other_side = 'DOWN' if side == 'UP' else 'UP'
        
        # Only improve if we have a position on this side
        if my_qty == 0:
            return False, 0, "No position to improve"
        
        # TIME-BASED BALANCE ENFORCEMENT
        # After grace period, aggressively enforce balance
        time_since_first = time.time() - self.first_trade_time if self.first_trade_time > 0 else 0
        strict_balance_mode = time_since_first > self.balance_enforcement_delay
        
        # CRITICAL: HARD STOP - Never improve if we're already 1.2x+ larger than other side
        # v11: Much stricter - balance is EVERYTHING!
        if other_qty > 0:
            current_ratio = my_qty / other_qty
            if current_ratio > 1.15:  # Hard stop at 1.15x imbalance (WAS 2.0x)
                return False, 0, f"🚨 HARD STOP: ratio {current_ratio:.2f}x - MUST balance {other_side} first!"
            
            # CRITICAL: DELTA PROTECTION - Stricter after grace period
            current_delta_pct = abs(my_qty - other_qty) / (my_qty + other_qty) * 100
            
            # After 30s: Don't improve larger side if delta >5% (strict mode)
            # Before 30s: Allow up to 15% delta (flexible mode)
            max_allowed_delta = self.ideal_balance_delta_pct if strict_balance_mode else self.max_flex_delta_pct
            
            if current_delta_pct > max_allowed_delta and my_qty > other_qty:
                mode_str = "STRICT" if strict_balance_mode else "FLEX"
                return False, 0, f"🚨 DELTA STOP ({mode_str}): {current_delta_pct:.1f}% > {max_allowed_delta:.1f}% - MUST balance {other_side} first!"
        
        # CRITICAL: Stop buying if opposite side is too expensive for ANY hedge
        # Even break-even requires pair <= 1.00, so if opposite > stop_threshold,
        # we'd need avg < (1.00 - opposite) which may be impossible
        if opposing_price is not None and opposing_price > self.stop_buying_opposite_price:
            max_avg_for_breakeven = 1.00 - opposing_price
            if my_avg > max_avg_for_breakeven:
                return False, 0, f"🛑 STOP: opposite ${opposing_price:.2f} too expensive, need avg <${max_avg_for_breakeven:.2f}"
        
        # Check if current price is below our average (ANY amount!)
        price_improvement = my_avg - price
        price_improvement_pct = price_improvement / my_avg if my_avg > 0 else 0
        
        # DEBUG
        print(f"  🔍 [IMPROVE CHECK {side}] price=${price:.3f} avg=${my_avg:.3f} diff=${price_improvement:.3f} ({price_improvement_pct*100:.1f}%)")
        
        if price >= my_avg:
            return False, 0, f"Price ${price:.3f} >= avg ${my_avg:.3f}"
        
        if price_improvement < self.improvement_threshold and price_improvement_pct < self.min_improvement_pct:
            return False, 0, f"Improvement only ${price_improvement:.3f} ({price_improvement_pct*100:.1f}%) - need >{self.improvement_threshold} or >{self.min_improvement_pct*100}%"
        
        # If we have both sides, check imbalance
        if other_qty > 0:
            current_ratio = my_qty / other_qty
            # If we're already the larger side by a lot, only improve if profit is not locked
            if current_ratio > self.max_imbalance_for_improvement:
                # Allow if we don't have locked profit yet, or if improvement is significant
                if self.locked_profit > 0 and price_improvement_pct < 0.10:  # < 10% improvement
                    return False, 0, f"Already ahead: {current_ratio:.2f}x ratio"
        available = min(self.affordable_cash(self.improvement_trade_pct), self.max_single_trade)
        
        if available < self.min_trade_size:
            return False, 0, f"Insufficient budget ${available:.2f}"

        desired_spend = available
        if other_qty == 0:
            last_price = self.last_improvement_price.get(side)
            if last_price is not None and price > last_price - self.improvement_step_price + 1e-6:
                required = max(0.0, last_price - self.improvement_step_price)
                return False, 0, f"Need price <= ${required:.3f} for next ladder"
            
            # Accelerated ladder: spend more at lower prices
            ladder_spend = self.ladder_tiers[-1][1]  # default
            for threshold, spend_amt in self.ladder_tiers:
                if price <= threshold:
                    ladder_spend = spend_amt
                    break
            desired_spend = min(ladder_spend, available)
            
            # Check max spend per side limit
            if my_cost + desired_spend > self.max_spend_per_side:
                remaining_allowed = self.max_spend_per_side - my_cost
                if remaining_allowed < self.min_trade_size:
                    return False, 0, f"Max spend per side ${self.max_spend_per_side:.0f} reached"
                desired_spend = min(desired_spend, remaining_allowed)
            
            # Check break-even hedge reserve
            if self.enable_breakeven_check:
                can_spend, allowed_spend, reason = self._check_breakeven_reserve(side, price, my_qty, my_cost, desired_spend)
                if not can_spend:
                    return False, 0, reason
                if allowed_spend < desired_spend:
                    print(f"  💰 [BREAKEVEN CAP] {reason}")
                    desired_spend = allowed_spend
        
        spend = self.capped_spend_until_ok(
            side,
            price,
            desired_spend=desired_spend,
            opposing_price=other_avg if other_qty > 0 else None,
            fraction=1.0,
            min_spend=self.min_trade_size
        )

        throttle_ok, allowed_spend, throttle_reason = self._evaluate_improvement_throttle(side, spend)
        throttled = False
        if not throttle_ok:
            if allowed_spend >= self.min_trade_size:
                spend = allowed_spend
                throttled = True
            else:
                return False, 0, throttle_reason

        if spend < self.min_trade_size:
            return False, 0, f"Insufficient reserve for improvement"

        qty = spend / price
        
        # Simulate the new average
        new_cost = my_cost + (qty * price)
        new_qty = my_qty + qty
        new_avg = new_cost / new_qty
        avg_improvement = my_avg - new_avg
        
        # Check: new average must be meaningfully better
        if avg_improvement < 0.01:
            return False, 0, f"Would only improve avg by ${avg_improvement:.3f}"
        
        # Calculate new hedge requirement
        old_max_hedge_price = 1.0 - my_avg
        new_max_hedge_price = 1.0 - new_avg
        window_expansion = new_max_hedge_price - old_max_hedge_price

        reason = f"📈 IMPROVE: +${spend:.2f} avg ${my_avg:.3f}→${new_avg:.3f} | hedge window expands by ${window_expansion:.3f}"
        if throttled:
            reason += f" | throttle cap ${self.improvement_spend_cap:.0f}/{self.improvement_spend_window:.0f}s"

        return True, qty, reason

    def simulate_buy(self, side: str, price: float, qty: float) -> tuple:
        cost = price * qty
        if side == 'UP':
            new_cost_up = self.cost_up + cost
            new_qty_up = self.qty_up + qty
            new_avg_up = new_cost_up / new_qty_up
            new_avg_down = self.avg_down
        else:
            new_cost_down = self.cost_down + cost
            new_qty_down = self.qty_down + qty
            new_avg_down = new_cost_down / new_qty_down
            new_avg_up = self.avg_up
        
        if new_avg_up == 0 or new_avg_down == 0:
            return (new_avg_up if side == 'UP' else new_avg_down, 0.0)
        return (new_avg_up if side == 'UP' else new_avg_down, new_avg_up + new_avg_down)
    
    def calculate_smart_hedge(self, hedge_price: float) -> dict:
        """
        Beregner smart hedge når pair_cost > 1.0
        
        Strategi: Kjøp FLERE shares på hedge-siden slik at:
        - Hvis hedge-siden vinner: Vi går i PLUSS
        - Hvis original-siden vinner: Vi går i MINUS (men begrenset)
        
        Formel for break-even på hedge-siden:
        qty_hedge = existing_cost / (1 - hedge_price)
        
        For å gå i PLUSS, kjøper vi litt mer enn break-even.
        """
        # Determine which side we're hedging
        if self.qty_up > 0 and self.qty_down == 0:
            existing_qty = self.qty_up
            existing_cost = self.cost_up
            existing_avg = self.avg_up
            hedge_side = 'DOWN'
        elif self.qty_down > 0 and self.qty_up == 0:
            existing_qty = self.qty_down
            existing_cost = self.cost_down
            existing_avg = self.avg_down
            hedge_side = 'UP'
        else:
            return {'viable': False, 'reason': 'Need unhedged position'}
        
        # Can't smart hedge if price >= 1.0
        if hedge_price >= 1.0:
            return {'viable': False, 'reason': 'Hedge price too high'}
        
        # Calculate break-even hedge quantity
        # If hedge wins: qty_hedge - existing_cost - (qty_hedge * hedge_price) = 0
        # qty_hedge * (1 - hedge_price) = existing_cost
        # qty_hedge = existing_cost / (1 - hedge_price)
        breakeven_qty = existing_cost / (1 - hedge_price)
        breakeven_cost = breakeven_qty * hedge_price
        
        # Add buffer for profit (10% more shares)
        profit_buffer = 1.10
        smart_qty = breakeven_qty * profit_buffer
        smart_cost = smart_qty * hedge_price
        
        # Calculate outcomes
        total_cost = existing_cost + smart_cost
        
        # If hedge side wins:
        pnl_if_hedge_wins = smart_qty - total_cost
        
        # If original side wins:
        pnl_if_original_wins = existing_qty - total_cost
        
        # Check viability
        result = {
            'viable': False,
            'hedge_side': hedge_side,
            'existing_qty': existing_qty,
            'existing_cost': existing_cost,
            'hedge_price': hedge_price,
            'breakeven_qty': breakeven_qty,
            'smart_qty': smart_qty,
            'smart_cost': smart_cost,
            'total_cost': total_cost,
            'pnl_if_hedge_wins': pnl_if_hedge_wins,
            'pnl_if_original_wins': pnl_if_original_wins,
            'reason': ''
        }
        
        # Check constraints
        if hedge_price > self.smart_hedge_max_price:
            result['reason'] = f'Price ${hedge_price:.2f} > max ${self.smart_hedge_max_price}'
            return result
        
        if smart_cost > self.smart_hedge_max_spend:
            result['reason'] = f'Cost ${smart_cost:.2f} > max ${self.smart_hedge_max_spend}'
            return result
        
        if smart_cost > self.cash * 0.5:  # Don't spend more than 50% of cash
            result['reason'] = f'Cost ${smart_cost:.2f} > 50% of cash'
            return result
        
        if pnl_if_hedge_wins < self.smart_hedge_min_profit:
            result['reason'] = f'Profit ${pnl_if_hedge_wins:.2f} < min ${self.smart_hedge_min_profit}'
            return result
        
        # Check worst case loss is acceptable
        if abs(pnl_if_original_wins) > self.max_loss_per_market * 3:  # Allow 3x max loss for smart hedge
            result['reason'] = f'Worst loss ${pnl_if_original_wins:.2f} too high'
            return result
        
        result['viable'] = True
        result['reason'] = 'Smart hedge viable!'
        return result
    
    def should_buy(self, side: str, price: float, other_price: float, is_rebalance: bool = False, is_emergency: bool = False, time_to_close: float = None) -> tuple:
        """
        GABAGOOL v7 - RECOVERY MODE ENABLED
        
        THE ONLY WAY TO GUARANTEE PROFIT:
        - pair_cost (avg_UP + avg_DOWN) < $1.00
        - qty_UP ≈ qty_DOWN (balanced positions)
        
        RECOVERY MODE: When pair_cost > $1.00, allow high imbalance
        to aggressively cost-average and get pair_cost under $1.00
        """
        if self.market_status != 'open':
            return False, 0, "Market not open"
        
        now = time.time()
        cooldown = self.cooldown_seconds / 2 if is_rebalance else self.cooldown_seconds
        if now - self.last_trade_time < cooldown:
            return False, 0, "Cooldown active"
        
        my_qty = self.qty_up if side == 'UP' else self.qty_down
        my_cost = self.cost_up if side == 'UP' else self.cost_down
        my_avg = my_cost / my_qty if my_qty > 0 else 0
        other_qty = self.qty_down if side == 'UP' else self.qty_up
        other_cost = self.cost_down if side == 'UP' else self.cost_up
        other_avg = other_cost / other_qty if other_qty > 0 else 0
        other_side = 'DOWN' if side == 'UP' else 'UP'
        
        # === POSITION SIZE LIMIT ===
        total_spent = self.cost_up + self.cost_down
        max_total_spend = self.starting_balance * self.max_position_pct
        remaining_budget = max_total_spend - total_spent
        
        if remaining_budget <= self.min_trade_size and not is_emergency and not (my_qty == 0 and other_qty > 0):
            return False, 0, f"Position limit reached (spent ${total_spent:.0f})"
        
        # ============================================================
        # GOAL: min(qty_up, qty_down) > total_spent  AND  pair_cost < $1
        # This guarantees profit regardless of outcome!
        # ============================================================
        
        # === PHASE 1: ENTRY - Buy cheap side first ===
        if my_qty == 0 and other_qty == 0:
            if price > self.cheap_threshold:
                return False, 0, f"First trade needs price < ${self.cheap_threshold}"
            
            if time_to_close is not None and time_to_close < 180:
                return False, 0, f"Only {time_to_close:.0f}s left - too late to start"
            
            max_spend = min(self.initial_trade_usd, self.max_single_trade, remaining_budget, self.cash)
            qty = max_spend / price
            self.first_trade_time = now
            return True, qty, f"🎯 ENTRY @ ${price:.3f}"
        
        # === PHASE 2: HEDGE or IMPROVE - One side only ===
        if my_qty == 0 and other_qty > 0:
            potential_pair = other_avg + price
            
            # === NEW: POSITION IMPROVEMENT ===
            # Before accepting a bad hedge, check if we can IMPROVE the existing position!
            # If the OTHER side (which we own) has a better price now, buy more to lower avg
            other_side_local = 'DOWN' if side == 'UP' else 'UP'
            should_improve, improve_qty, improve_reason = self.should_improve_position(other_side_local, other_price, opposing_price=price)
            
            if should_improve and potential_pair > 0.96:
                # The hedge would be expensive - try improving instead!
                return False, 0, f"⏳ Hedge expensive (pair ${potential_pair:.3f}). {improve_reason}"
            
            # After 10 seconds, refuse pair > $1.00 unless it is mathematically recoverable
            market_elapsed = MARKET_WINDOW_SECONDS - time_to_close if time_to_close is not None else 0.0
            if market_elapsed > 10 and potential_pair > 1.0:
                target_qty = other_qty
                hedge_cost = target_qty * price
                remaining_after = remaining_budget - hedge_cost

                if side == 'UP':
                    qty_up_after = self.qty_up + target_qty
                    cost_up_after = self.cost_up + hedge_cost
                    qty_down_after = self.qty_down
                    cost_down_after = self.cost_down
                    up_price = price
                    down_price = other_price
                else:
                    qty_down_after = self.qty_down + target_qty
                    cost_down_after = self.cost_down + hedge_cost
                    qty_up_after = self.qty_up
                    cost_up_after = self.cost_up
                    up_price = other_price
                    down_price = price

                recoverable = remaining_after >= self.min_trade_size and self.can_recover_pair_cost(
                    up_price,
                    down_price,
                    remaining_after,
                    qty_up_after,
                    cost_up_after,
                    qty_down_after,
                    cost_down_after
                )

                if not recoverable:
                    return False, 0, f"⛔ REFUSE hedge: pair ${potential_pair:.3f} > $1.00 after {market_elapsed:.0f}s"
            
            # Match qty to balance
            target_qty = other_qty
            cost_needed = target_qty * price
            # Allow larger hedge if it locks profit, otherwise cap at max_single_trade
            will_lock_profit = (min(target_qty, other_qty) - (self.cost_up + self.cost_down + cost_needed)) > 0
            if will_lock_profit:
                max_spend = min(cost_needed, self.cash * 0.8)  # Can spend more to lock profit
            else:
                max_spend = min(cost_needed, self.max_single_trade, self.cash * 0.3)  # Limited otherwise
            qty = max_spend / price
            
            if qty < 1.0:
                return False, 0, f"Not enough cash to hedge"
            
            return True, qty, f"🔒 HEDGE @ ${price:.3f} (pair: ${potential_pair:.2f})"
        
        # === PHASE 3: OPTIMIZE - Build toward guaranteed profit ===
        current_pair_cost = self.pair_cost
        total_spent = self.cost_up + self.cost_down
        min_qty = min(self.qty_up, self.qty_down)
        fees = self.calculate_total_fees()
        
        # THE KEY METRIC: guaranteed_profit = min_qty - total_spent - fees
        guaranteed_profit = min_qty - total_spent - fees
        
        # Current ratio (1.0 = perfectly balanced)
        ratio = my_qty / other_qty if other_qty > 0 else 1.0
        
        # TARGET: pair_cost < $0.93 to ensure profit after fees!
        TARGET_PAIR_COST = 0.93
        
        # === SUCCESS CHECK ===
        # v12: NEVER stop trading just because we have profit!
        # Always look for ways to GROW profit until market closes
        profit_growth_mode = guaranteed_profit > 0 and current_pair_cost < TARGET_PAIR_COST
        # REMOVED: No longer stop when profit is locked - keep growing!
        # if profit_growth_mode and not self.allow_profit_growth:
        #     return False, 0, f"✅ DONE! profit=${guaranteed_profit:.2f}, pair=${current_pair_cost:.3f}"

        def profit_growth_allows(new_locked: float, new_pair_cost: float) -> bool:
            """v12: Much more permissive - allow trades that don't HURT us significantly"""
            if not profit_growth_mode:
                return True
            # Allow if pair cost improves
            if new_pair_cost < current_pair_cost:
                return True
            # Allow if locked profit increases
            if new_locked > guaranteed_profit:
                return True
            # v12: Also allow if we maintain at least 90% of locked profit
            # and pair cost doesn't get too bad (< 0.99)
            profit_preserved = new_locked >= guaranteed_profit * 0.90
            pair_still_safe = new_pair_cost < 0.99
            if profit_preserved and pair_still_safe:
                return True
            return False
        
        # === NEED TO IMPROVE ===
        # Strategy: Buy whichever side helps reach the goal
        
        # RULE 0: EMERGENCY STOP - Never allow ratio > 1.35x (v11: was 2.5x)
        if ratio > self.emergency_ratio:
            return False, 0, f"🚨 EMERGENCY STOP: Ratio {ratio:.2f}x > {self.emergency_ratio}x - MUST buy {other_side} first!"
        
        # RULE 0.5: CRITICAL - Don't buy larger side when ratio > 1.2x (v11: was 2.0x)
        if ratio > self.critical_ratio and my_qty > other_qty:
            return False, 0, f"🛑 CRITICAL: Ratio {ratio:.2f}x - cannot buy {side}, must balance with {other_side} first"
        
        # RULE 1: Don't exceed ratio of 1.10 under normal conditions (v11: was 1.3)
        if ratio > 1.10 and my_qty > other_qty:
            return False, 0, f"⛔ Ratio {ratio:.2f}x - need to buy {other_side}"
        
        # RULE 1.5: PRIORITIZE balance when position delta > 5%
        # This ensures we maintain tight qty balance for guaranteed profit
        current_delta_pct = abs(my_qty - other_qty) / (my_qty + other_qty) * 100 if (my_qty + other_qty) > 0 else 0
        
        if current_delta_pct > self.ideal_balance_delta_pct and my_qty < other_qty:
            # We're the lagging side and imbalance exceeds 5% - prioritize catching up
            # Target: reduce delta to 5% or less
            target_my_qty = other_qty * (1 - self.ideal_balance_delta_pct / 100) / (1 + self.ideal_balance_delta_pct / 100)
            qty_to_balance = max(0, target_my_qty - my_qty)
            
            if qty_to_balance > 0:
                max_spend = min(self.cash * 0.4, qty_to_balance * price, remaining_budget)
                qty = max_spend / price
                
                if qty * price >= self.min_trade_size:
                    new_locked = self.locked_profit_after_buy(side, price, qty)
                    new_my_qty = my_qty + qty
                    new_delta_pct = abs(new_my_qty - other_qty) / (new_my_qty + other_qty) * 100
                    new_avg, new_pair_cost = self.simulate_buy(side, price, qty)
                    if profit_growth_allows(new_locked, new_pair_cost):
                        return True, qty, f"⚖️ BALANCE (5% rule): delta {current_delta_pct:.1f}%→{new_delta_pct:.1f}%, locked ${guaranteed_profit:.2f}→${new_locked:.2f}"
        
        # RULE 2: If we're the lagging side, buy to catch up (increases min_qty!)
        # v11: Trigger rebalance earlier at 0.98 ratio (WAS 0.95)
        if ratio < 0.98:
            qty_to_balance = other_qty - my_qty
            max_spend = min(self.cash * 0.7, qty_to_balance * price, remaining_budget)  # 70% of cash for balance
            qty = max_spend / price
            
            if qty * price >= self.min_trade_size:
                new_locked = self.locked_profit_after_buy(side, price, qty)
                new_ratio = (my_qty + qty) / other_qty
                new_avg, new_pair_cost = self.simulate_buy(side, price, qty)
                if profit_growth_allows(new_locked, new_pair_cost):
                    return True, qty, f"⚖️ BALANCE: ratio {ratio:.2f}→{new_ratio:.2f}, locked ${guaranteed_profit:.2f}→${new_locked:.2f}"
        
        # RULE 3: If pair_cost >= TARGET ($0.97), only buy if it reduces pair_cost
        if current_pair_cost >= TARGET_PAIR_COST:
            new_avg, new_pair_cost = self.simulate_buy(side, price, 10)
            
            if new_pair_cost >= current_pair_cost:
                return False, 0, f"⏳ pair=${current_pair_cost:.3f} (need <${TARGET_PAIR_COST}), price ${price:.3f} won't help"
            
            # Good! This trade reduces pair_cost toward target
            max_spend = min(self.cash * 0.4, self.max_single_trade, remaining_budget)
            qty = max_spend / price
            
            if qty * price >= self.min_trade_size:
                new_avg, new_pair_cost = self.simulate_buy(side, price, qty)
                new_locked = self.locked_profit_after_buy(side, price, qty)
                if profit_growth_allows(new_locked, new_pair_cost):
                    return True, qty, f"📉 REDUCE: pair ${current_pair_cost:.3f}→${new_pair_cost:.3f}, locked ${guaranteed_profit:.2f}→${new_locked:.2f}"
        
        # RULE 4: If pair_cost < TARGET, buy cheap to grow position
        # v11: Only allow growth if almost balanced (ratio <= 1.05, was 1.15)
        if price <= self.cheap_threshold and ratio <= 1.05:
            max_spend = min(self.cash * 0.3, self.max_single_trade, remaining_budget)
            qty = max_spend / price
            
            if qty * price >= self.min_trade_size:
                new_locked = self.locked_profit_after_buy(side, price, qty)
                new_avg, new_pair_cost = self.simulate_buy(side, price, qty)
                if new_locked > guaranteed_profit and profit_growth_allows(new_locked, new_pair_cost):
                    return True, qty, f"💰 CHEAP @ ${price:.3f}: locked ${guaranteed_profit:.2f}→${new_locked:.2f}"
        
        return False, 0, f"⏳ pair=${current_pair_cost:.3f} (target <${TARGET_PAIR_COST}), locked=${guaranteed_profit:.2f}, ratio={ratio:.2f}x"
    
    def execute_buy(self, side: str, price: float, qty: float, timestamp: str, mode: str = None, reason: str = None):
        cost = price * qty
        # No cash limit for testing - just track spending
        
        self.cash -= cost
        self.trade_count += 1
        self.last_trade_time = time.time()
        
        if side == 'UP':
            self.qty_up += qty
            self.cost_up += cost
        else:
            self.qty_down += qty
            self.cost_down += cost

        # Update ladder anchor for this side
        self.last_improvement_price[side] = price
        
        # Update mode if provided
        if mode:
            self.current_mode = mode
        if reason:
            self.mode_reason = reason
        
        self.trade_log.append({
            'time': timestamp,
            'side': 'BUY',
            'token': side,
            'price': price,
            'qty': qty,
            'cost': cost
        })
        
        if len(self.trade_log) > 20:
            self.trade_log = self.trade_log[-20:]
        
        return True

    def reconcile_buy(self, side: str, paper_qty: float, paper_price: float,
                      actual_qty: float, actual_price: float) -> None:
        """Correct paper qty/cost/cash to match the actual live fill.
        Called after a live BUY order returns its real filled_qty/fill_price.
        paper_qty/paper_price = what simulate_fill estimated.
        actual_qty/actual_price = what the exchange actually filled (0 if failed).
        """
        delta_qty  = actual_qty  - paper_qty
        delta_cost = (actual_qty * actual_price) - (paper_qty * paper_price)
        if side == 'UP':
            self.qty_up  = max(0.0, self.qty_up  + delta_qty)
            self.cost_up = max(0.0, self.cost_up + delta_cost)
        else:
            self.qty_down  = max(0.0, self.qty_down  + delta_qty)
            self.cost_down = max(0.0, self.cost_down + delta_cost)
        # Refund (or charge) the cash difference
        self.cash -= delta_cost
        if abs(delta_qty) > 0.001 or abs(delta_cost) > 0.001:
            import logging as _lg
            _lg.getLogger(__name__).info(
                '[reconcile_buy] %s paper=%.3f@%.4f actual=%.3f@%.4f Δqty=%.3f Δcost=%.4f',
                side, paper_qty, paper_price, actual_qty, actual_price, delta_qty, delta_cost)

    def _attempt_profit_growth(self, up_price: float, down_price: float, locked_profit: float, pair_cost: float, remaining_budget: float, timestamp: str) -> List[tuple]:
        """
        PROFIT GROWTH MODE
        
        After securing locked profit, continue buying strategically to maximize upside.
        
        Strategy:
        1. Identify favorable side (better avg OR higher probability)
        2. Use limited budget (% of locked profit)
        3. Only buy if it improves expected value
        4. Stop if pair cost approaches danger zone
        
        Returns: list of trades made
        """
        trades = []
        
        # Calculate growth budget - use available budget
        # v12: More aggressive - use 70% of remaining budget
        growth_budget = min(
            remaining_budget * 0.70,  # Use up to 70% of remaining budget per trade (WAS 50%)
            self.growth_max_single_trade,
            self.affordable_cash(0.70) # Ensure we have actual cash
        )
        
        if growth_budget < self.min_trade_size:
            return trades
        
        # Determine which side to favor
        # Factor 1: Market probability (price indicates probability)
        prob_up = up_price
        prob_down = down_price
        
        # Factor 2: Better average
        avg_advantage_up = self.avg_down - self.avg_up if self.avg_down > 0 and self.avg_up > 0 else 0
        avg_advantage_down = self.avg_up - self.avg_down if self.avg_up > 0 and self.avg_down > 0 else 0
        
        # Factor 3: Expected value calculation
        total_spent = self.cost_up + self.cost_down
        fees = self.calculate_total_fees()
        ev_up = (prob_up * self.qty_up) - total_spent - fees
        ev_down = (prob_down * self.qty_down) - total_spent - fees
        
        # Score each side
        up_score = 0
        down_score = 0
        
        if self.growth_favor_probability:
            up_score += prob_up * 100
            down_score += prob_down * 100
        
        if self.growth_favor_better_avg:
            up_score += avg_advantage_up * 50
            down_score += avg_advantage_down * 50
        
        # Bonus for side that's below its average (can improve avg further)
        if up_price < self.avg_up:
            discount = (self.avg_up - up_price) / self.avg_up
            up_score += discount * 30
        if down_price < self.avg_down:
            discount = (self.avg_down - down_price) / self.avg_down
            down_score += discount * 30
        
        # Choose side to grow
        # v12: Lower score requirement from 5 to 2 - be more willing to grow
        if up_score > down_score and up_score > 2:
            growth_side = 'UP'
            growth_price = up_price
            opposing_price = down_price
            reason = f"Growing UP: prob={prob_up:.0%}, avg_adv=${avg_advantage_up:.3f}, score={up_score:.1f}"
        elif down_score > 2:
            growth_side = 'DOWN'
            growth_price = down_price
            opposing_price = up_price
            reason = f"Growing DOWN: prob={prob_down:.0%}, avg_adv=${avg_advantage_down:.3f}, score={down_score:.1f}"
        else:
            return trades  # No clear advantage
        
        # Calculate qty to buy
        qty = growth_budget / growth_price
        
        if qty < 0.5:
            return trades
        
        # Simulate the trade - check if pair cost stays safe
        if growth_side == 'UP':
            new_cost_up = self.cost_up + growth_budget
            new_qty_up = self.qty_up + qty
            new_avg_up = new_cost_up / new_qty_up
            new_pair_cost = new_avg_up + self.avg_down
        else:
            new_cost_down = self.cost_down + growth_budget
            new_qty_down = self.qty_down + qty
            new_avg_down = new_cost_down / new_qty_down
            new_pair_cost = self.avg_up + new_avg_down
        
        # Safety check: don't worsen pair cost too much
        # Dynamic limit: be more aggressive when locked profit < $3
        max_allowed_pair = (
            self.growth_max_pair_cost_low_profit 
            if locked_profit < self.min_target_locked_profit 
            else self.growth_max_pair_cost
        )
        
        if new_pair_cost > max_allowed_pair:
            print(f"⚠️ [GROWTH BLOCKED] Would push pair ${pair_cost:.3f}→${new_pair_cost:.3f} > ${max_allowed_pair:.3f}")
            return trades

        # Guardrail: one-sided growth must not destroy locked profit (tail-loss protection)
        new_locked = self.locked_profit_after_buy(growth_side, growth_price, qty)
        if new_locked < self.growth_min_locked_after_trade - 1e-9:
            print(f"⚠️ [GROWTH BLOCKED] Would reduce locked ${locked_profit:.2f}→${new_locked:.2f} (< ${self.growth_min_locked_after_trade:.2f})")
            return trades
        
        # Check reserves
        ok, reserve_reason = self.reserve_ok(growth_side, growth_price, qty, opposing_price)
        if not ok:
            print(f"⚠️ [GROWTH BLOCKED] {reserve_reason}")
            return trades
        
        # Execute growth trade
        self.current_mode = 'profit_growth'
        self.mode_reason = f'Growing position: {reason}'
        
        if self.execute_buy(growth_side, growth_price, qty, timestamp):
            trades.append((growth_side, growth_price, qty))
            print(f"📈 [PROFIT GROWTH] {reason}")
            print(f"   Bought {qty:.1f} {growth_side} @ ${growth_price:.3f} (${growth_budget:.2f})")
            print(f"   pair: ${pair_cost:.3f}→${new_pair_cost:.3f} | locked was ${locked_profit:.2f}")
        
        return trades
    
    def check_and_trade(self, up_price: float, down_price: float, timestamp: str, time_to_close: float = None, up_bid: Optional[float] = None, down_bid: Optional[float] = None):
        """
        GABAGOOL v9 - ULTRA AGGRESSIVE PROFIT HUNTER
        
        RULE: If locked profit < 0, ALWAYS try to buy something to improve it!
        Buy small amounts constantly until profit is locked.
        
        GOAL: min(qty_up, qty_down) > total_spent + fees
        """
        trades_made = []
        
        # Cooldown check
        now = time.time()
        if now - self.last_trade_time < self.cooldown_seconds:
            return trades_made
        
        total_spent = self.cost_up + self.cost_down
        budget_limit = self.starting_balance * self.max_position_pct
        remaining_budget = max(0, budget_limit - total_spent)
        
        # === LOSS PROTECTION: Only abandon if mathematically impossible to profit ===
        if self.qty_up > 0 and self.qty_down > 0:
            # ABANDON only if pair cost makes profit impossible
            if self.pair_cost > self.abandon_threshold_pair_cost:
                print(f"🛑 [ABANDON] Pair cost ${self.pair_cost:.3f} > ${self.abandon_threshold_pair_cost:.2f} - mathematically unprofitable")
                return trades_made
        
        # Calculate conservative mode status for spend adjustments
        unrealized_for_mode = 0
        if self.qty_up > 0 or self.qty_down > 0:
            min_qty = min(self.qty_up, self.qty_down) if self.qty_up > 0 and self.qty_down > 0 else 0
            fees = self.calculate_total_fees()
            unrealized_for_mode = min_qty - total_spent - fees
        in_conservative_mode = unrealized_for_mode < self.conservative_mode_loss_threshold
        
        # === NO POSITION - ENTRY ===
        if self.qty_up == 0 and self.qty_down == 0:
            cheaper_side = 'UP' if up_price <= down_price else 'DOWN'
            cheaper_price = min(up_price, down_price)
            opposing_price = down_price if cheaper_side == 'UP' else up_price
            
            # ENTRY STRATEGY: Enter if cheapest side is below threshold
            potential_pair = cheaper_price + opposing_price
            
            # Only skip if BOTH sides are very expensive
            if potential_pair > 1.05:
                print(f"⛔ [SKIP ENTRY] pair would be ${potential_pair:.3f} > $1.05 - both sides too expensive")
                return trades_made
            
            # Enter if cheapest side is reasonably priced (< $0.48)
            if cheaper_price <= self.cheap_threshold:
                max_spend = self.capped_spend(min(self.initial_trade_usd, self.max_single_trade))
                # Be even more conservative if we're tracking losses in other markets
                if in_conservative_mode:
                    max_spend = min(max_spend, self.initial_trade_usd * 0.5)
                if max_spend >= self.min_trade_size:
                    qty = max_spend / cheaper_price
                    if qty >= 1.0:
                        ok, reason = self.reserve_ok(cheaper_side, cheaper_price, qty, opposing_price)
                        if not ok:
                            print(f"⚠️ [ENTRY BLOCKED] {reason}")
                            return trades_made
                        self.first_trade_time = now
                        self.current_mode = 'entry'
                        self.mode_reason = f'Starting with {cheaper_side} @ ${cheaper_price:.3f}'
                        if self.execute_buy(cheaper_side, cheaper_price, qty, timestamp):
                            trades_made.append((cheaper_side, cheaper_price, qty))
                            print(f"🎯 [ENTRY] Bought {qty:.1f} {cheaper_side} @ ${cheaper_price:.3f}")
            return trades_made
        
        # === ONLY ONE SIDE - HEDGE OR IMPROVE! ===
        if self.qty_up > 0 and self.qty_down == 0:
            potential_pair = self.avg_up + down_price
            
            # ABANDON only if potential pair makes profit impossible
            if potential_pair > self.abandon_threshold_pair_cost:
                print(f"🛑 [ABANDON ONE-SIDED UP] Potential pair ${potential_pair:.3f} > ${self.abandon_threshold_pair_cost:.2f} - mathematically unprofitable")
                return trades_made
            
            # CRITICAL: Dynamic pair threshold based on urgency AND imbalance
            # Normal: require profit (pair < 0.99)
            # Fallback: accept break-even (pair <= 1.00) when time is short or price is extreme
            # EMERGENCY: accept up to pair 1.05 when ratio > 3x to prevent catastrophic imbalance
            current_ratio = self.qty_down / self.qty_up if self.qty_up > 0 else 999
            emergency_imbalance = current_ratio > self.emergency_hedge_ratio
            
            urgent_time = time_to_close is not None and time_to_close < self.breakeven_time_threshold
            urgent_price = down_price > self.breakeven_price_threshold
            
            if emergency_imbalance:
                MAX_ACCEPTABLE_PAIR = 1.05
                print(f"🚨 [EMERGENCY HEDGE] Ratio {current_ratio:.1f}x - accepting pair up to $1.05!")
            elif urgent_time or urgent_price:
                MAX_ACCEPTABLE_PAIR = self.max_acceptable_pair_breakeven
                urgency_reason = f"time={time_to_close:.0f}s" if urgent_time else f"price=${down_price:.2f}"
                print(f"⏰ [URGENT MODE] Accepting break-even hedge ({urgency_reason})")
            else:
                MAX_ACCEPTABLE_PAIR = self.max_acceptable_pair_profit
            
            # === CONTINUOUS POSITION IMPROVEMENT CHECK ===
            # Always check if we can lower avg_UP - this widens the hedge window
            should_improve, improve_qty, improve_reason = self.should_improve_position('UP', up_price, opposing_price=down_price)
            
            # DEBUG
            print(f"  → should_improve={should_improve}, improve_qty={improve_qty:.1f}, reason={improve_reason}")
            print(f"  → remaining_budget=${remaining_budget:.2f}, min_trade=${self.min_trade_size}")
            
            # If price dropped enough below avg, ALWAYS buy to lower avg (even if hedge is possible)
            force_improve = should_improve and self.avg_up > 0 and up_price <= self.avg_up * (1 - self.force_improve_pct)

            # === CONTINUOUS IMPROVEMENT - CHECK EVERY TICK ===
            # But respect balance enforcement after grace period (no force improve when one-sided)
            time_since_first = time.time() - self.first_trade_time if self.first_trade_time > 0 else 0
            strict_balance_mode = time_since_first > self.balance_enforcement_delay
            
            if force_improve:
                # After 30s, block FORCE IMPROVE on one-sided positions (must hedge first)
                if strict_balance_mode:
                    print(f"⏱️ [FORCE IMPROVE UP BLOCKED] After {self.balance_enforcement_delay}s - must hedge DOWN first (balance priority)")
                else:
                    ok, reason = self.reserve_ok('UP', up_price, improve_qty, down_price)
                    if not ok:
                        print(f"⚠️ [FORCE IMPROVE UP BLOCKED] {reason}")
                    else:
                        self.current_mode = 'improve'
                        self.mode_reason = f'Lowering UP avg from ${self.avg_up:.3f} @ ${up_price:.3f}'
                        if self.execute_buy('UP', up_price, improve_qty, timestamp):
                            trades_made.append(('UP', up_price, improve_qty))
                            self.record_improvement_spend('UP', up_price * improve_qty)
                            new_max_hedge = 1.0 - self.avg_up
                            print(f"🔥 [FORCE IMPROVE UP] Bought {improve_qty:.1f} UP @ ${up_price:.3f} | "
                                  f"avg_UP now ${self.avg_up:.3f} | hedge window <${new_max_hedge:.3f}")
                            return trades_made

            # If pair would exceed $1, try to improve first
            if potential_pair > 1.00:
                if should_improve:
                    ok, reason = self.reserve_ok('UP', up_price, improve_qty, down_price)
                    if ok:
                        if self.execute_buy('UP', up_price, improve_qty, timestamp):
                            trades_made.append(('UP', up_price, improve_qty))
                            self.record_improvement_spend('UP', up_price * improve_qty)
                            new_max_hedge = 1.0 - self.avg_up
                            print(f"📈 [IMPROVE UP] Bought {improve_qty:.1f} UP @ ${up_price:.3f} | "
                                  f"avg_UP now ${self.avg_up:.3f} | hedge window <${new_max_hedge:.3f}")
                            return trades_made
                    else:
                        print(f"⚠️ [IMPROVE UP BLOCKED] {reason}")
                
                # REFUSE HEDGE if pair > $1.00 - this guarantees loss!
                print(f"⛔ [REFUSE HEDGE] pair ${potential_pair:.3f} > $1.00 would guarantee loss - waiting for better DOWN price")
                return trades_made
            
            # === HEDGE! BUY DOWN! ===
            if potential_pair < 0.99:
                hedge_type = "PROFIT"
            elif potential_pair <= 1.00:
                hedge_type = "BREAK-EVEN"
            else:
                hedge_type = "HIGH (will improve)"
            print(f"✅ [HEDGE - {hedge_type}] pair ${potential_pair:.3f} - BUYING DOWN!")
            
            target_qty = self.qty_up
            desired_spend = target_qty * down_price
            # CRITICAL FIX: Increase hedge budget to ensure proper balancing!
            # Was fraction=0.6, now 0.85 to allow buying enough of expensive side
            max_spend = self.capped_spend(desired_spend, fraction=0.85)
            # CRITICAL FIX: Increase hedge cap from $20 to $40 for better balance
            max_spend = min(max_spend, 40.0)  # Cap hedge at $40 (was $20)
            qty = max_spend / down_price if down_price > 0 else 0.0
            
            # 🛡️ DELTA PROTECTION: Limit hedge qty to avoid excessive imbalance
            # If budget-limited qty creates >15% delta, warn but proceed (budget constrained)
            if qty > 0:
                new_delta_pct = abs(self.qty_up - qty) / (self.qty_up + qty) * 100
                if new_delta_pct > 15.0:
                    print(f"   ⚠️ HEDGE CREATES {new_delta_pct:.1f}% delta (budget limited to ${max_spend:.2f})")
            
            if qty >= 0.5 and max_spend >= self.min_trade_size:
                ok, reason = self.reserve_ok('DOWN', down_price, qty, up_price)
                if not ok:
                    print(f"⚠️ [HEDGE BLOCKED] {reason}")
                    return trades_made
                self.current_mode = 'hedge'
                self.mode_reason = f'Hedging UP with DOWN @ ${down_price:.3f} (pair: ${potential_pair:.3f})'
                if self.execute_buy('DOWN', down_price, qty, timestamp):
                    trades_made.append(('DOWN', down_price, qty))
                    print(f"🔒 [HEDGE] Bought {qty:.1f} DOWN @ ${down_price:.3f} | spend ${max_spend:.2f} | pair: ${self.pair_cost:.3f}")
            return trades_made
        
        if self.qty_down > 0 and self.qty_up == 0:
            potential_pair = up_price + self.avg_down
            
            # ABANDON only if potential pair makes profit impossible
            if potential_pair > self.abandon_threshold_pair_cost:
                print(f"🛑 [ABANDON ONE-SIDED DOWN] Potential pair ${potential_pair:.3f} > ${self.abandon_threshold_pair_cost:.2f} - mathematically unprofitable")
                return trades_made
            
            # CRITICAL: Dynamic pair threshold based on urgency AND imbalance
            # Normal: require profit (pair < 0.99)
            # Fallback: accept break-even (pair <= 1.00) when time is short or price is extreme
            # EMERGENCY: accept up to pair 1.05 when ratio > 3x to prevent catastrophic imbalance
            current_ratio = self.qty_up / self.qty_down if self.qty_down > 0 else 999
            emergency_imbalance = current_ratio > self.emergency_hedge_ratio
            
            urgent_time = time_to_close is not None and time_to_close < self.breakeven_time_threshold
            urgent_price = up_price > self.breakeven_price_threshold
            
            if emergency_imbalance:
                MAX_ACCEPTABLE_PAIR = 1.05
                print(f"🚨 [EMERGENCY HEDGE] Ratio {current_ratio:.1f}x - accepting pair up to $1.05!")
            elif urgent_time or urgent_price:
                MAX_ACCEPTABLE_PAIR = self.max_acceptable_pair_breakeven
                urgency_reason = f"time={time_to_close:.0f}s" if urgent_time else f"price=${up_price:.2f}"
                print(f"⏰ [URGENT MODE] Accepting break-even hedge ({urgency_reason})")
            else:
                MAX_ACCEPTABLE_PAIR = self.max_acceptable_pair_profit
            
            # Calculate the max UP price we can afford for profit
            max_up_for_profit = MAX_ACCEPTABLE_PAIR - self.avg_down
            
            # DEBUG: Always show state for one-sided positions
            print(f"🔵 [ONE-SIDED DOWN] qty={self.qty_down:.1f} avg=${self.avg_down:.3f} | "
                  f"UP=${up_price:.3f} | pair=${potential_pair:.3f} | max_UP=${max_up_for_profit:.3f} | budget=${remaining_budget:.2f}")
            
            # === POSITION IMPROVEMENT - CHECK FIRST! ===
            should_improve, improve_qty, improve_reason = self.should_improve_position('DOWN', down_price, opposing_price=up_price)
            
            force_improve = should_improve and self.avg_down > 0 and down_price <= self.avg_down * (1 - self.force_improve_pct)

            # === ALWAYS IMPROVE ON DEEP DISCOUNT - BEFORE CHECKING HEDGE ===
            # But respect balance enforcement after grace period (no force improve when one-sided)
            time_since_first = time.time() - self.first_trade_time if self.first_trade_time > 0 else 0
            strict_balance_mode = time_since_first > self.balance_enforcement_delay
            
            if force_improve:
                # After 30s, block FORCE IMPROVE on one-sided positions (must hedge first)
                if strict_balance_mode:
                    print(f"⏱️ [FORCE IMPROVE DOWN BLOCKED] After {self.balance_enforcement_delay}s - must hedge UP first (balance priority)")
                else:
                    ok, reason = self.reserve_ok('DOWN', down_price, improve_qty, up_price)
                    if not ok:
                        print(f"⚠️ [FORCE IMPROVE DOWN BLOCKED] {reason}")
                    else:
                        if self.execute_buy('DOWN', down_price, improve_qty, timestamp):
                            trades_made.append(('DOWN', down_price, improve_qty))
                            self.record_improvement_spend('DOWN', down_price * improve_qty)
                            new_max_up = MAX_ACCEPTABLE_PAIR - self.avg_down
                            print(f"🔥 [FORCE IMPROVE DOWN] Bought {improve_qty:.1f} DOWN @ ${down_price:.3f} | "
                                  f"avg_DOWN ${self.avg_down:.3f} | can now pay UP <${new_max_up:.3f}")
                            return trades_made

            if potential_pair > 1.00:
                # Try to improve first
                if should_improve:
                    ok, reason = self.reserve_ok('DOWN', down_price, improve_qty, up_price)
                    if ok:
                        if self.execute_buy('DOWN', down_price, improve_qty, timestamp):
                            trades_made.append(('DOWN', down_price, improve_qty))
                            self.record_improvement_spend('DOWN', down_price * improve_qty)
                            new_max_up = 1.0 - self.avg_down
                            print(f"📈 [IMPROVE DOWN] Bought {improve_qty:.1f} DOWN @ ${down_price:.3f} | "
                                  f"avg_DOWN ${self.avg_down:.3f} | can now pay UP <${new_max_up:.3f}")
                            return trades_made
                    else:
                        print(f"⚠️ [IMPROVE DOWN BLOCKED] {reason}")
                
                # REFUSE HEDGE if pair > $1.00
                print(f"⛔ [REFUSE HEDGE] pair ${potential_pair:.3f} > $1.00 would guarantee loss - waiting for better UP price")
                return trades_made
            
            # === HEDGE! BUY UP! ===
            if potential_pair < 0.99:
                hedge_type = "PROFIT"
            elif potential_pair <= 1.00:
                hedge_type = "BREAK-EVEN"
            else:
                hedge_type = "HIGH (will improve)"
            print(f"✅ [HEDGE - {hedge_type}] pair ${potential_pair:.3f} - BUYING UP!")
            
            target_qty = self.qty_down
            desired_spend = target_qty * up_price
            # CRITICAL FIX: Increase hedge budget to ensure proper balancing!
            # Was fraction=0.6, now 0.85 to allow buying enough of expensive side
            max_spend = self.capped_spend(desired_spend, fraction=0.85)
            # CRITICAL FIX: Increase hedge cap from $20 to $40 for better balance
            max_spend = min(max_spend, 40.0)  # Cap hedge at $40 (was $20)
            qty = max_spend / up_price if up_price > 0 else 0.0
            
            # 🛡️ DELTA PROTECTION: Limit hedge qty to avoid excessive imbalance
            # If budget-limited qty creates >15% delta, warn but proceed (budget constrained)
            if qty > 0:
                new_delta_pct = abs(self.qty_down - qty) / (self.qty_down + qty) * 100
                if new_delta_pct > 15.0:
                    print(f"   ⚠️ HEDGE CREATES {new_delta_pct:.1f}% delta (budget limited to ${max_spend:.2f})")
            
            print(f"   target_qty={target_qty:.1f} | afford=${max_spend:.2f} | qty={qty:.1f}")
            
            if qty >= 0.5 and max_spend >= self.min_trade_size:
                ok, reason = self.reserve_ok('UP', up_price, qty, down_price)
                if not ok:
                    print(f"⚠️ [HEDGE BLOCKED] {reason}")
                    return trades_made
                self.current_mode = 'hedge'
                self.mode_reason = f'Hedging DOWN with UP @ ${up_price:.3f} (pair: ${potential_pair:.3f})'
                if self.execute_buy('UP', up_price, qty, timestamp):
                    trades_made.append(('UP', up_price, qty))
                    print(f"🔒 [HEDGE] Bought {qty:.1f} UP @ ${up_price:.3f} | spend ${max_spend:.2f} | pair: ${self.pair_cost:.3f}")
            else:
                print(f"⚠️ [SKIP HEDGE] qty {qty:.1f} < 0.5 minimum")
            return trades_made
        
        # === HAVE BOTH SIDES - OPTIMIZE UNTIL PROFIT LOCKED ===
        min_qty = min(self.qty_up, self.qty_down)
        fees = self.calculate_total_fees()
        locked = min_qty - total_spent - fees
        pair_cost = self.pair_cost
        
        # Show current position status
        if locked < -50:
            print(f"⚠️ [LARGE LOSS] unrealized=${locked:.2f}, pair=${pair_cost:.3f}, spent=${total_spent:.0f} - seeking improvements")
        
        # ✅ PROFIT SECURED - Continue improving if possible
        profit_is_locked = locked > 0.02
        if profit_is_locked:
            self.current_mode = 'arbitrage'
            self.mode_reason = f'Profit locked ${locked:.2f} - seeking improvements'
            print(f"✅ [PROFIT LOCKED] locked=${locked:.2f} - looking for improvements")
            
            # === PROFIT GROWTH MODE ===
            # Keep trading until window closes IF we can improve profit.
            # 1) Prefer paired compounding (increases locked profit without adding tail risk)
            if self.enable_profit_growth and locked >= self.min_locked_for_growth:
                pair_trades = self._attempt_pair_profit_compound(up_price, down_price, locked, pair_cost, remaining_budget, timestamp)
                if pair_trades:
                    trades_made.extend(pair_trades)
                    return trades_made

            # 2) Optional one-sided growth (only if it keeps locked profit >= 0 and stays under pair-cost guard)
            if self.enable_profit_growth and locked >= self.min_locked_for_growth and pair_cost < self.growth_max_pair_cost:
                growth_trades = self._attempt_profit_growth(up_price, down_price, locked, pair_cost, remaining_budget, timestamp)
                if growth_trades:
                    trades_made.extend(growth_trades)
                    return trades_made
            
            # v12: Even with profit locked, keep looking for favorable opportunities!
            # If price drops significantly below our average, we can grow profit further
            print(f"📈 [GROW SCAN] Searching for growth opportunities (pair=${pair_cost:.3f}, locked=${locked:.2f})")
        
        # ⚠️ PROFIT NOT LOCKED - MUST IMPROVE!
        if remaining_budget < self.min_trade_size:
            print(f"⚠️ [NO BUDGET] locked=${locked:.2f} but only ${remaining_budget:.2f} budget left!")
            return trades_made
        
        # === DYNAMIC WORST-SIDE PRIORITIZATION ===
        # v12: Always run this, even with profit locked - we want to GROW profit!
        worst_side, severity, recommended_spend, priority_reason = self.evaluate_worst_positioned_side(up_price, down_price)
        
        # Only log when not profit-locked (reduce noise)
        if not profit_is_locked:
            print(f"🔍 [PRIORITY CHECK] worst={worst_side}, severity={severity:.1f}, spend=${recommended_spend:.2f}, reason={priority_reason}")
        
        # Reduce spending if in conservative mode
        if in_conservative_mode and recommended_spend > 0:
            recommended_spend = min(recommended_spend, self.max_single_trade * 0.5)
            if not profit_is_locked:
                print(f"   Conservative mode: reduced spend to ${recommended_spend:.2f}")
        
        if worst_side and severity > 2.0 and recommended_spend >= self.min_trade_size:
                worst_price = up_price if worst_side == 'UP' else down_price
                opp_price = down_price if worst_side == 'UP' else up_price
                worst_avg = self.avg_up if worst_side == 'UP' else self.avg_down
                
                print(f"   worst_price=${worst_price:.3f}, worst_avg=${worst_avg:.3f}")
                
                # Only execute if price is actually below average
                if worst_price < worst_avg:
                    qty_to_buy = recommended_spend / worst_price
                    
                    if qty_to_buy >= 1.0:
                        ok, reason = self.reserve_ok(worst_side, worst_price, qty_to_buy, opp_price)
                        if not ok:
                            print(f"⚠️ [PRIORITY BLOCKED] {reason}")
                        else:
                            # Simulate result
                            if worst_side == 'UP':
                                new_qty_up = self.qty_up + qty_to_buy
                                new_cost_up = self.cost_up + recommended_spend
                                new_avg_up = new_cost_up / new_qty_up
                                new_pair = new_avg_up + self.avg_down
                                new_min_qty = min(new_qty_up, self.qty_down)
                            else:
                                new_qty_down = self.qty_down + qty_to_buy
                                new_cost_down = self.cost_down + recommended_spend
                                new_avg_down = new_cost_down / new_qty_down
                                new_pair = self.avg_up + new_avg_down
                                new_min_qty = min(self.qty_up, new_qty_down)
                            
                            new_locked = new_min_qty - (total_spent + recommended_spend) - self.calculate_total_fees()
                            improvement = new_locked - locked
                            
                            # Execute if it helps
                            if new_pair <= pair_cost or improvement > 0:
                                self.current_mode = 'priority_fix'
                                self.mode_reason = priority_reason
                                if self.execute_buy(worst_side, worst_price, qty_to_buy, timestamp):
                                    trades_made.append((worst_side, worst_price, qty_to_buy))
                                    print(f"🎯 [PRIORITY FIX] {priority_reason}")
                                    print(f"   Bought {qty_to_buy:.1f} {worst_side} @ ${worst_price:.3f} | ${recommended_spend:.2f}")
                                    print(f"   pair ${pair_cost:.3f}→${new_pair:.3f} | locked ${locked:.2f}→${new_locked:.2f}")
                                    return trades_made
        
        # === CRITICAL FIX: Check for "CATCH UP + COST AVERAGE" opportunity ===
        # When one side is behind AND price is below average, this is a DOUBLE WIN:
        # 1. Increases min_qty (the smaller side grows)
        # 2. Lowers pair_cost (buying below average lowers the average)
        
        # Find the lagging side
        if self.qty_up < self.qty_down:
            lagging_side = 'UP'
            lagging_qty = self.qty_up
            lagging_avg = self.avg_up
            lagging_price = up_price
            leading_qty = self.qty_down
        else:
            lagging_side = 'DOWN'
            lagging_qty = self.qty_down
            lagging_avg = self.avg_down
            lagging_price = down_price
            leading_qty = self.qty_up
        
        imbalance_ratio = leading_qty / lagging_qty if lagging_qty > 0 else 999
        price_below_avg = lagging_price < lagging_avg
        price_discount = lagging_avg - lagging_price
        
        # DEBUG: Show current state
        if locked < 0:
            print(f"🔴 [LOSING] pair=${pair_cost:.3f} | {lagging_side}: {lagging_qty:.1f} @ ${lagging_avg:.3f} (price ${lagging_price:.3f}) | "
                  f"imbalance={imbalance_ratio:.1f}x | locked=${locked:.2f} | budget=${remaining_budget:.2f}")
        
        # === AGGRESSIVE CATCH-UP when imbalanced ===
        # CRITICAL: If locked < 0, we MUST buy the lagging side to increase min_qty
        # Even if price is above average, it's better than guaranteed loss!
        needs_urgent_balance = locked < -10 and imbalance_ratio > 1.3
        
        if (price_below_avg and imbalance_ratio > 1.3) or needs_urgent_balance:
            # Calculate how much we need to catch up
            qty_gap = leading_qty - lagging_qty
            
            # Calculate optimal buy: enough to significantly improve both metrics
            # Start with catching up to at least 80% of leading side
            target_catch_up = leading_qty * 0.8 - lagging_qty
            if target_catch_up > 0:
                cost_to_catch_up = target_catch_up * lagging_price
                # INCREASED: Was 0.15, now 0.25 - allow larger catch-up trades!
                max_spend = self.capped_spend(cost_to_catch_up, fraction=0.25)
                # INCREASED: Cap at $50 for catch-ups (was $30) - allows better hedging!
                max_spend = min(max_spend, 50.0)
                qty_to_buy = max_spend / lagging_price if lagging_price > 0 else 0.0
                
                if qty_to_buy >= 1.0 and max_spend >= self.min_trade_size:
                    opp_price = down_price if lagging_side == 'UP' else up_price
                    ok, reason = self.reserve_ok(lagging_side, lagging_price, qty_to_buy, opp_price)
                    if not ok:
                        print(f"⚠️ [CATCH-UP BLOCKED] {reason}")
                        return trades_made
                    # Simulate the result
                    if lagging_side == 'UP':
                        new_qty_up = self.qty_up + qty_to_buy
                        new_cost_up = self.cost_up + (qty_to_buy * lagging_price)
                        new_avg_up = new_cost_up / new_qty_up
                        new_pair_cost = new_avg_up + self.avg_down
                        new_min_qty = min(new_qty_up, self.qty_down)
                        new_total_spent = new_cost_up + self.cost_down
                    else:
                        new_qty_down = self.qty_down + qty_to_buy
                        new_cost_down = self.cost_down + (qty_to_buy * lagging_price)
                        new_avg_down = new_cost_down / new_qty_down
                        new_pair_cost = self.avg_up + new_avg_down
                        new_min_qty = min(self.qty_up, new_qty_down)
                        new_total_spent = self.cost_up + new_cost_down
                    
                    new_locked = new_min_qty - new_total_spent
                    improvement = new_locked - locked
                    
                    # Only execute if it actually improves locked profit
                    if improvement > 0.5 and new_pair_cost < pair_cost:
                        if new_pair_cost > 1.0:
                            remaining_after = remaining_budget - (qty_to_buy * lagging_price)
                            if remaining_after < self.min_trade_size or not self.can_recover_pair_cost(
                                up_price,
                                down_price,
                                remaining_after,
                                new_qty_up,
                                new_cost_up,
                                new_qty_down,
                                new_cost_down
                            ):
                                return trades_made
                        # Allow if it improves locked profit or pair cost
                        if profit_is_locked and improvement < 0.01 and new_pair_cost >= pair_cost:
                            return trades_made
                        self.current_mode = 'rebalance'
                        self.mode_reason = f'Catching up {lagging_side}: ratio {imbalance_ratio:.1f}x → balanced'
                        if self.execute_buy(lagging_side, lagging_price, qty_to_buy, timestamp):
                            trades_made.append((lagging_side, lagging_price, qty_to_buy))
                            print(f"🚀 [CATCH-UP] Bought {qty_to_buy:.1f} {lagging_side} @ ${lagging_price:.3f} (below avg ${lagging_avg:.3f}) | "
                                  f"pair ${pair_cost:.3f}→${new_pair_cost:.3f} | locked ${locked:.2f}→${new_locked:.2f} (+${improvement:.2f})")
                            return trades_made
        
        # === Standard optimization: try different trade sizes ===
        best_side = None
        best_qty = 0
        best_improvement = 0
        best_new_pair = pair_cost
        best_new_locked = locked
        
        # Try larger trade sizes - limited by bankroll availability
        trade_sizes = [1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0]  # USD amounts
        affordable_for_tests = self.affordable_cash(0.5)
        
        for try_side, try_price in [('UP', up_price), ('DOWN', down_price)]:
            my_qty = self.qty_up if try_side == 'UP' else self.qty_down
            my_avg = self.avg_up if try_side == 'UP' else self.avg_down
            other_qty = self.qty_down if try_side == 'UP' else self.qty_up
            opp_price = down_price if try_side == 'UP' else up_price
            
            # ⚖️ BALANCE CHECK: Don't buy larger side when position delta > 15% (max flex)
            # Allow rebalancing even at high delta if pair_cost is critical
            if other_qty > 0 and my_qty > other_qty:
                current_delta_pct = abs(my_qty - other_qty) / (my_qty + other_qty) * 100
                if current_delta_pct > self.max_flex_delta_pct:
                    # Already over 15% delta - don't make it worse unless emergency
                    if pair_cost < 1.05:  # Only skip if we're not in deep trouble
                        continue
            
            for trade_usd in trade_sizes:
                if trade_usd > affordable_for_tests:
                    continue
                
                # Evaluate only sizes we can fund
                test_qty = trade_usd / try_price
                if test_qty < 0.5:
                    continue
                ok, _ = self.reserve_ok(try_side, try_price, test_qty, opp_price)
                if not ok:
                    continue
                
                # Simulate the trade
                if try_side == 'UP':
                    new_qty_up = self.qty_up + test_qty
                    new_qty_down = self.qty_down
                    new_cost_up = self.cost_up + trade_usd
                    new_cost_down = self.cost_down
                else:
                    new_qty_down = self.qty_down + test_qty
                    new_qty_up = self.qty_up
                    new_cost_down = self.cost_down + trade_usd
                    new_cost_up = self.cost_up
                
                new_avg_up = new_cost_up / new_qty_up
                new_avg_down = new_cost_down / new_qty_down
                new_pair_cost = new_avg_up + new_avg_down
                
                fee_up = self.calculate_fee(new_avg_up, new_qty_up)
                fee_down = self.calculate_fee(new_avg_down, new_qty_down)
                new_fees = fee_up + fee_down
                
                new_total_spent = new_cost_up + new_cost_down
                new_min_qty = min(new_qty_up, new_qty_down)
                new_locked = new_min_qty - new_total_spent - new_fees
                
                improvement = new_locked - locked
                
                # Accept if it improves locked profit
                if improvement > best_improvement:
                    # STRATEGIC POSITIONING when pair_cost > $1.00:
                    # Prioritize buying the side with HIGHER average (lowers it the most)
                    # Even if pair cost temporarily increases, it positions us better for recovery
                    if pair_cost >= 1.00:
                        # Which side has higher average?
                        higher_avg_side = 'UP' if self.avg_up > self.avg_down else 'DOWN'
                        
                        # Only accept trades on the higher-avg side, OR trades that reduce pair_cost
                        if new_pair_cost > pair_cost:
                            # Pair cost got worse - only accept if buying the high-avg side
                            if try_side != higher_avg_side:
                                continue
                            # And only if we're buying significantly below average
                            if try_price >= my_avg * 0.90:  # Must be at least 10% discount
                                continue
                            print(f"   💡 [STRATEGIC] Buying {try_side} (high avg ${my_avg:.3f}) @ ${try_price:.3f} to position for recovery")

                    if new_pair_cost > 1.0:
                        remaining_after = remaining_budget - trade_usd
                        if remaining_after < self.min_trade_size or not self.can_recover_pair_cost(
                            up_price,
                            down_price,
                            remaining_after,
                            new_qty_up,
                            new_cost_up,
                            new_qty_down,
                            new_cost_down
                        ):
                            continue

                    if profit_is_locked:
                        # Allow if it improves pair cost OR locked profit (even slightly)
                        if new_pair_cost >= pair_cost and improvement < 0.01:
                            continue
                    
                    # Prefer buying the lagging side
                    is_lagging_side = (my_qty < other_qty)
                    
                    # Bonus for buying below average (cost averaging)
                    is_below_avg = (try_price < my_avg)
                    
                    # Give priority to trades that are both lagging AND below avg
                    effective_improvement = improvement
                    if is_lagging_side and is_below_avg:
                        effective_improvement *= 1.5  # 50% bonus
                    elif is_lagging_side or is_below_avg:
                        effective_improvement *= 1.2  # 20% bonus
                    
                    if effective_improvement > best_improvement:
                        best_side = try_side
                        best_qty = test_qty
                        best_improvement = improvement  # Store actual improvement
                        best_new_pair = new_pair_cost
                        best_new_locked = new_locked
        
        # Execute the best trade if we found one that helps
        if best_side and best_improvement > 0.05:  # At least 5 cents improvement
            best_price = up_price if best_side == 'UP' else down_price
            opp_price = down_price if best_side == 'UP' else up_price
            ok, reason = self.reserve_ok(best_side, best_price, best_qty, opp_price)
            if not ok:
                print(f"⚠️ [OPTIMIZE BLOCKED] {reason}")
                return trades_made
            self.current_mode = 'optimize'
            self.mode_reason = f'Optimizing {best_side} for +${best_improvement:.2f} locked profit'
            if self.execute_buy(best_side, best_price, best_qty, timestamp):
                trades_made.append((best_side, best_price, best_qty))
                print(
                    f"💰 [OPTIMIZE] Bought {best_qty:.1f} {best_side} @ ${best_price:.3f} | "
                    f"pair ${pair_cost:.3f}→${best_new_pair:.3f} | locked ${locked:.2f}→${best_new_locked:.2f} (+${best_improvement:.2f})"
                )
        
        return trades_made
    
    def resolve_market(self, outcome: str):
        self.market_status = 'resolved'
        self.resolution_outcome = outcome
        
        if outcome == 'UP':
            self.payout = self.qty_up * 1.0
        else:
            self.payout = self.qty_down * 1.0
        
        total_cost = self.cost_up + self.cost_down
        fees = self.calculate_total_fees()
        self.last_fees_paid = fees
        self.final_pnl_gross = self.payout - total_cost
        self.final_pnl = self.final_pnl_gross - fees
        net_payout = max(0.0, self.payout - fees)
        
        # Add net payout back to cash
        self.cash += net_payout
        
        return self.final_pnl
    
    def close_market(self):
        self.market_status = 'closed'
    
    def get_state(self) -> dict:
        # Calculate hedge windows - max price we can pay for the other side
        # Use 0.99 threshold (not 1.0) to account for fees
        max_hedge_up = 0.99 - self.avg_down if self.avg_down > 0 else 0.99
        max_hedge_down = 0.99 - self.avg_up if self.avg_up > 0 else 0.99
        
        return {
            'qty_up': self.qty_up,
            'qty_down': self.qty_down,
            'cost_up': self.cost_up,
            'cost_down': self.cost_down,
            'avg_up': self.avg_up,
            'avg_down': self.avg_down,
            'pair_cost': self.pair_cost,
            'locked_profit': self.locked_profit,
            'best_case_profit': self.best_case_profit,
            'qty_ratio': self.qty_ratio,
            # Position delta: |A-B| / (A+B) * 100
            'balance_pct': (abs(self.qty_up - self.qty_down) / (self.qty_up + self.qty_down) * 100) if (self.qty_up + self.qty_down) > 0 else 0,
            'is_balanced': ((abs(self.qty_up - self.qty_down) / (self.qty_up + self.qty_down) * 100) <= 5.0) if (self.qty_up + self.qty_down) > 0 else False,
            'trade_count': self.trade_count,
            'market_status': self.market_status,
            'resolution_outcome': self.resolution_outcome,
            'final_pnl': self.final_pnl,
            'final_pnl_gross': self.final_pnl_gross,
            'fees_paid': self.last_fees_paid,
            'payout': self.payout,
            # Hedge window info - max price for profitable hedge (pair < $0.99)
            'max_hedge_up': max_hedge_up,    # Max UP price for profit if we only have DOWN
            'max_hedge_down': max_hedge_down,  # Max DOWN price for profit if we only have UP
            # Trading mode
            'current_mode': self.current_mode,
            'mode_reason': self.mode_reason
        }


class MarketTracker:
    """Tracks a single market"""
    
    def __init__(self, slug: str, asset: str, cash_ref: dict, market_budget: float, exec_sim: ExecutionSimulator = None):
        self.slug = slug
        self.asset = asset
        self.up_token_id = None
        self.down_token_id = None
        self.window_start = None
        self.window_end = None
        self.up_price = None
        self.down_price = None
        self.last_up_bid = 0.0
        self.last_down_bid = 0.0
        self.market_budget = market_budget
        self.executor = None  # Per-market LiveExecutor (isolated state)
        # NEW: Use ArbitrageStrategy with shared ExecutionSimulator
        self.paper_trader = ArbitrageStrategy(market_budget=market_budget, starting_balance=market_budget, exec_sim=exec_sim)
        self.paper_trader.cash_ref = cash_ref  # Share cash reference
        self.initialized = False
        self.last_update = 0
        self.up_orderbook = {'bids': [], 'asks': []}
        self.down_orderbook = {'bids': [], 'asks': []}
        self.orderbook_updated_at = 0.0
        self.spot_open_price: Optional[float] = None  # BTC spot at market open
        self.event_start_time: Optional[datetime] = None  # When 5-min window actually starts (from Polymarket API)
        self.reference_price: Optional[float] = None  # BTC price at window start (the resolution reference)
        self.reference_price_source: str = ''  # How we got the reference price
        # Live inventory tracking — actual shares confirmed filled on-chain
        self.live_inventory: Dict[str, float] = {'UP': 0.0, 'DOWN': 0.0}
        # Tracks SELL qty that was already executed by stranded-sweep so normal
        # strategy SELL reconcile does not incorrectly restore the rung.
        self._recent_sweep_fill: Dict[str, Dict[str, float]] = {
            'UP': {'qty': 0.0, 'price': 0.0, 'ts': 0.0},
            'DOWN': {'qty': 0.0, 'price': 0.0, 'ts': 0.0},
        }
        # Per-side cooldown after allowance failures to avoid spam retries
        self._sell_backoff_until: Dict[str, float] = {'UP': 0.0, 'DOWN': 0.0}
        # Set to True after first successful CLOB balance sync (avoids repeat at every tick)
        self._balances_synced: bool = False
        # Independent live P&L tracking — unaffected by paper reconciliation bugs
        self.live_cost_total: float = 0.0      # sum of all confirmed live BUY costs ($)
        self.live_proceeds_total: float = 0.0  # sum of all confirmed live SELL proceeds ($)


class MultiMarketBot:
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    
    def __init__(self, starting_balance: float = 4000.0, per_market_budget: float = 1000.0):
        self.initial_starting_balance = starting_balance
        # Allow overrides via env to match Render/VPS config.
        try:
            starting_balance = float(os.getenv('STARTING_BALANCE', starting_balance))
        except Exception:
            pass
        try:
            per_market_budget = float(os.getenv('PER_MARKET_BUDGET', per_market_budget))
        except Exception:
            pass
        self.initial_per_market_budget = per_market_budget
        self.starting_balance = starting_balance
        self.per_market_budget = per_market_budget
        self.cash_ref = {'balance': starting_balance}
        self.active_markets: Dict[str, MarketTracker] = {}
        self._market_tasks: Dict[str, asyncio.Task] = {}  # One independent task per market
        self._no_market_log_at: Dict[str, float] = {}  # throttle 'no market found' log per asset
        self._ws_feed = None  # PolymarketWSFeed — started in data_loop
        self.history: List[dict] = []
        self.websockets = set()
        # Spot price state
        self.last_btc_spot: Optional[float] = None
        self.last_spot_prices: Dict[str, float] = {}  # Per-asset spot cache
        self.spot_fetch_errors: int = 0
        self.running = True
        self.update_count = 0
        self.manual_markets_loaded = False
        self.trade_log: List[dict] = []
        _is_live_boot = os.getenv('LIVE_TRADING', 'false').strip().lower() == 'true'
        self.paused = os.getenv('LIVE_TRADING', 'false').strip().lower() == 'true'  # Paper: auto-start, Live: paused
        self.mirror_mode = False
        self.live_armed = False          # Always start disarmed — user must manually activate
        self._pre_arm_markets: set = set()  # market slugs open at arm time — excluded from live
        # Shared execution simulator — stats persist across all markets
        # Uses LiveExecutor when available (reads LIVE_TRADING from .env)
        if LiveExecutor is not None:
            self.exec_sim = LiveExecutor(latency_ms=25.0, max_slippage_pct=2.0)
        else:
            self.exec_sim = ExecutionSimulator(latency_ms=25.0, max_slippage_pct=2.0)
    
    async def load_manual_markets(self, session: aiohttp.ClientSession):
        """Load manually specified markets"""
        if self.manual_markets_loaded:
            return
        
        self.manual_markets_loaded = True
        
        for slug in MANUAL_MARKETS:
            if slug in self.active_markets:
                continue
            if any(h['slug'] == slug for h in self.history):
                continue
            
            # Determine asset from slug
            asset = None
            for a in SUPPORTED_ASSETS:
                cfg = ASSET_MARKET_CONFIG.get(a, {'suffix': MARKET_WINDOW_SUFFIX})
                if slug.startswith(f'{a}-updown-{cfg["suffix"]}-'):
                    asset = a
                    break
            
            if not asset:
                print(f"⚠️ Unknown asset in slug: {slug}")
                continue
            
            try:
                url = f"{self.GAMMA_API_URL}/events?slug={slug}"
                async with session.get(url) as response:
                    if response.status == 200:
                        events = await response.json()
                        
                        if not events:
                            print(f"⚠️ Market not found: {slug}")
                            continue
                        
                        event = events[0]
                        markets = event.get('markets', [])
                        up_token, down_token = self._extract_tokens_from_markets(markets, target_slug=slug)
                        
                        if up_token and down_token:
                            asset_budget = self.per_market_budget
                            tracker = MarketTracker(slug, asset, self.cash_ref, asset_budget, self.exec_sim)
                            tracker.executor = LiveExecutor(latency_ms=25.0, max_slippage_pct=2.0) if LiveExecutor is not None else self.exec_sim
                            tracker.up_token_id = up_token
                            tracker.down_token_id = down_token
                            
                            end_date_str = event.get('endDate', '')
                            if end_date_str:
                                try:
                                    tracker.window_end = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                                except:
                                    pass
                            
                            tracker.initialized = True
                            # Pre-sync CLOB balances at discovery (off trade path)
                            _exec_sync = tracker.executor
                            if getattr(_exec_sync, 'live', False) and hasattr(_exec_sync, 'fetch_live_balances'):
                                if hasattr(_exec_sync, 'set_token_ids'):
                                    _exec_sync.set_token_ids(up_token, down_token)
                                try:
                                    _clob_bal = await _exec_sync.fetch_live_balances()
                                    tracker._balances_synced = True
                                    for _bs, _bb in _clob_bal.items():
                                        if _bb > 0.0:
                                            tracker.live_inventory[_bs] = _bb
                                except Exception:
                                    pass  # non-critical, will retry on first trade
                            self.active_markets[slug] = tracker
                            if hasattr(tracker.paper_trader, 'mirror_mode'):
                                tracker.paper_trader.mirror_mode = getattr(self, 'mirror_mode', False)
                            if self._ws_feed and tracker.up_token_id and tracker.down_token_id:
                                self._ws_feed.subscribe(tracker.up_token_id)
                                self._ws_feed.subscribe(tracker.down_token_id)
                            print(f"✅ Loaded market: {slug}")
                            print(f"   UP token: {up_token[:20]}...")
                            print(f"   DOWN token: {down_token[:20]}...")
                        else:
                            print(f"⚠️ Missing tokens for: {slug}")
                    else:
                        print(f"⚠️ Failed to fetch {slug}: status {response.status}")
            except Exception as e:
                print(f"Error loading manual market {slug}: {e}")

    @staticmethod
    def _compress_orderbook(book: dict, max_levels: Optional[int] = None) -> dict:
        if not isinstance(book, dict):
            return {'bids': [], 'asks': []}

        def _convert(levels, reverse):
            cleaned = []
            for level in list(levels):
                try:
                    price = float(level.get('price', 0))
                    size = float(level.get('size', 0))
                except (TypeError, ValueError):
                    continue
                if price <= 0 or size <= 0:
                    continue
                cleaned.append({'price': round(price, 4), 'size': round(size, 2)})
            cleaned.sort(key=lambda x: x['price'], reverse=reverse)
            return cleaned if max_levels is None else cleaned[:max_levels]

        return {
            'bids': _convert(book.get('bids', []), reverse=True),
            'asks': _convert(book.get('asks', []), reverse=False),
        }

    def _extract_tokens_from_markets(self, markets: List[dict], target_slug: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """Return UP/DOWN token ids, preferring the exact slug when available."""

        def _ensure_list(value):
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except Exception:
                    return []
            return value or []

        candidates = []
        if target_slug:
            for market in markets:
                if market.get('slug') == target_slug:
                    candidates = [market]
                    break
        if not candidates:
            candidates = markets

        up_token = None
        down_token = None

        for market in candidates:
            outcomes = _ensure_list(market.get('outcomes', []))
            tokens = _ensure_list(market.get('clobTokenIds', []))

            if outcomes and tokens and len(outcomes) >= 2 and len(tokens) >= 2:
                for idx, outcome in enumerate(outcomes):
                    outcome_name = str(outcome).lower()
                    if outcome_name == 'up':
                        up_token = tokens[idx]
                    elif outcome_name == 'down':
                        down_token = tokens[idx]

            if (not up_token or not down_token) and tokens:
                outcome_label = str(market.get('groupItemTitle', '')).lower()
                if 'up' in outcome_label and tokens:
                    up_token = tokens[0]
                elif 'down' in outcome_label and tokens:
                    down_token = tokens[0]

            if up_token and down_token:
                break

        return up_token, down_token
        
    async def discover_markets(self, session: aiohttp.ClientSession):
        """Discover active markets for all supported assets"""
        # First, load manual markets if any
        await self.load_manual_markets(session)
        
        for asset in SUPPORTED_ASSETS:
            # Get per-asset market window config
            cfg = ASSET_MARKET_CONFIG.get(asset, {'window_seconds': MARKET_WINDOW_SECONDS, 'suffix': MARKET_WINDOW_SUFFIX})
            window_seconds = cfg['window_seconds']
            window_suffix = cfg['suffix']
            
            # Calculate current and next windows for THIS asset
            now = int(time.time())
            current_window = (now // window_seconds) * window_seconds
            next_window = current_window + window_seconds
            
            # Skip if we already have an OPEN market for this asset
            # Resolved markets don't block new ones
            has_open_market = any(
                t.asset == asset and t.paper_trader.market_status == 'open'
                for t in self.active_markets.values()
            )
            if has_open_market:
                continue
            
            # Only track one market per asset at a time
            timestamps_to_check = [current_window, next_window]
            
            # Find one market for this asset
            for ts in timestamps_to_check:
                slug = f"{asset}-updown-{window_suffix}-{ts}"
                
                # Skip if already tracking or in history
                if slug in self.active_markets:
                    break  # Already have this one
                if any(h['slug'] == slug for h in self.history):
                    continue  # Already resolved, try next
                
                try:
                    # Use the direct slug endpoint
                    url = f"{self.GAMMA_API_URL}/events/slug/{slug}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as response:
                        if response.status != 200:
                            continue
                        
                        event = await response.json()
                        
                        if not event:
                            continue
                        
                        # Skip closed markets
                        if event.get('closed', False):
                            continue
                        
                        markets = event.get('markets', [])
                        up_token, down_token = self._extract_tokens_from_markets(markets, target_slug=slug)

                        if up_token and down_token:
                            asset_budget = self.per_market_budget
                            tracker = MarketTracker(slug, asset, self.cash_ref, asset_budget, self.exec_sim)
                            tracker.executor = LiveExecutor(latency_ms=25.0, max_slippage_pct=2.0) if LiveExecutor is not None else self.exec_sim
                            tracker.up_token_id = up_token
                            tracker.down_token_id = down_token
                            
                            end_date_str = event.get('endDate', '')
                            if end_date_str:
                                try:
                                    tracker.window_end = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                                except:
                                    pass
                            
                            # Parse eventStartTime — the exact start of the 5-min window
                            # The market resolves based on BTC price at start vs end of this window
                            event_start_str = ''
                            for m in markets:
                                event_start_str = m.get('eventStartTime', '')
                                if event_start_str:
                                    break
                            if not event_start_str:
                                event_start_str = event.get('startTime', '')
                            if event_start_str:
                                try:
                                    tracker.event_start_time = datetime.fromisoformat(event_start_str.replace('Z', '+00:00'))
                                    tracker.window_start = tracker.event_start_time
                                except:
                                    pass
                            
                            tracker.initialized = True
                            tracker.spot_open_price = None  # Reset for new market
                            tracker.reference_price = None
                            tracker.paper_trader.reset_predictor_for_new_market()
                            # Pre-sync CLOB balances at discovery (off trade path)
                            _exec_sync2 = tracker.executor
                            if getattr(_exec_sync2, 'live', False) and hasattr(_exec_sync2, 'fetch_live_balances'):
                                if hasattr(_exec_sync2, 'set_token_ids'):
                                    _exec_sync2.set_token_ids(up_token, down_token)
                                try:
                                    _clob_bal2 = await _exec_sync2.fetch_live_balances()
                                    tracker._balances_synced = True
                                    for _bs2, _bb2 in _clob_bal2.items():
                                        if _bb2 > 0.0:
                                            tracker.live_inventory[_bs2] = _bb2
                                except Exception:
                                    pass  # non-critical
                            self.active_markets[slug] = tracker
                            if hasattr(tracker.paper_trader, 'mirror_mode'):
                                tracker.paper_trader.mirror_mode = getattr(self, 'mirror_mode', False)
                            if self._ws_feed and tracker.up_token_id and tracker.down_token_id:
                                self._ws_feed.subscribe(tracker.up_token_id)
                                self._ws_feed.subscribe(tracker.down_token_id)
                            start_info = f" | starts {tracker.event_start_time.strftime('%H:%M:%S')}Z" if tracker.event_start_time else ""
                            print(f"🔍 Auto-discovered: {slug} (budget ${asset_budget:.0f}{start_info})")
                            break  # Found one for this asset, move to next asset
                except Exception as e:
                    print(f"⚠️ discover_markets [{asset}/{slug}]: {e}")
            # Log once per minute when no open market exists for this asset
            if not any(t.asset == asset and t.paper_trader.market_status == 'open'
                       for t in self.active_markets.values()):
                _now_m = time.time()
                if _now_m - self._no_market_log_at.get(asset, 0) > 60:
                    self._no_market_log_at[asset] = _now_m
                    print(f"[discover] no open market for {asset} — checked {timestamps_to_check}")
            await asyncio.sleep(0.3)  # space out requests per asset — avoid rate-limiting
    
    async def update_market(self, session: aiohttp.ClientSession, tracker: MarketTracker):
        """Update a single market's data"""
        if not tracker.initialized:
            return
        
        # Don't update resolved markets
        if tracker.paper_trader.market_status == 'resolved':
            return
        
        # Check if market window has ended
        now = datetime.now(timezone.utc)
        market_expired = tracker.window_end and now > tracker.window_end
        
        # If market expired, close it immediately and calculate PnL
        if market_expired and tracker.paper_trader.market_status == 'open':
            pt = tracker.paper_trader
            
            # Determine winner based on last prices (UP wins if UP price > DOWN price)
            up_price = tracker.up_price or 0.5
            down_price = tracker.down_price or 0.5
            if up_price > down_price:
                outcome = 'UP'
                payout = pt.qty_up  # $1 per UP share
            else:
                outcome = 'DOWN'
                payout = pt.qty_down  # $1 per DOWN share
            
            # Sync paper qty to actual live_inventory before computing payout
            _inv = tracker.live_inventory
            _live_up = _inv.get('UP', 0.0)
            _live_dn = _inv.get('DOWN', 0.0)
            _is_live_res = getattr(getattr(self, 'exec_sim', None), 'live', False)
            if (_live_up > 0 or _live_dn > 0) and _is_live_res:
                try:
                    if abs(pt.qty_up - _live_up) > 0.05 or abs(pt.qty_down - _live_dn) > 0.05:
                        print(f'[RESOLVE] Correcting paper→live qty: '
                              f'UP {pt.qty_up:.2f}→{_live_up:.2f}  DOWN {pt.qty_down:.2f}→{_live_dn:.2f}')
                    # LadderMateStrategy uses _pos[side].qty (read-only property on class);
                    # other strategies may have plain attributes — handle both.
                    if hasattr(pt, '_pos'):
                        pt._pos['UP'].qty   = _live_up
                        pt._pos['DOWN'].qty = _live_dn
                    else:
                        pt.qty_up   = _live_up
                        pt.qty_down = _live_dn
                    # Recalculate the expected payout with corrected qty
                    if outcome == 'UP':
                        payout = _live_up
                    else:
                        payout = _live_dn
                except Exception as _qe:
                    print(f'[RESOLVE] qty inject failed ({_qe}) — using paper qty')

            # Snapshot positions BEFORE resolve clears them
            snap_qty_up   = pt.qty_up
            snap_qty_down = pt.qty_down
            snap_cost_up  = pt.cost_up
            snap_cost_dn  = pt.cost_down
            snap_cash_out = pt.cash_out          # total $ spent on all buys
            snap_sell_in  = pt.cash_in           # $ received from rung sells so far

            pnl = pt.resolve_market(outcome)
            fees_paid = getattr(pt, 'last_fees_paid', 0.0)
            gross_pnl = getattr(pt, 'final_pnl_gross', pnl + fees_paid)
            net_payout = max(0.0, pt.payout - fees_paid)
            sell_proceeds = snap_sell_in          # cash from rung sells before resolution
            resolution_payout = pt.payout         # payout from winning shares at $1
            # Poll actual CLOB balance for winning side — live_inventory may be stale
            _live_win_qty = tracker.live_inventory.get(outcome, 0.0)
            if _is_live_res:
                _executor = getattr(tracker, 'executor', None) or getattr(self, 'exec_sim', None)
                if _executor and hasattr(_executor, '_async_get_balance') and hasattr(_executor, '_get_token_id'):
                    try:
                        _win_token = _executor._get_token_id(outcome)
                        if _win_token:
                            _clob_win = await _executor._async_get_balance(_win_token)
                            if _clob_win > 0.1 and abs(_clob_win - _live_win_qty) > 0.1:
                                print(f'[RESOLVE] CLOB balance correction: '
                                      f'live_inventory={_live_win_qty:.2f} CLOB={_clob_win:.2f}')
                                _live_win_qty = _clob_win
                                tracker.live_inventory[outcome] = _clob_win
                    except Exception as _pe:
                        print(f'[RESOLVE] CLOB balance poll failed: {_pe}')
            _live_net_pnl = round(_live_win_qty + tracker.live_proceeds_total - tracker.live_cost_total, 4) if _is_live_res else None

            print(f"\u26f3 [{tracker.asset.upper()}] Market closed: {outcome} won | "
                  f"Net: ${pnl:+.2f} | spent=${snap_cash_out:.2f} "
                  f"sells=${sell_proceeds:.2f} payout=${resolution_payout:.2f} "
                  f"(fees ${fees_paid:.2f})")

            # Add to history
            self.history.append({
                'resolved_at': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                'slug': tracker.slug,
                'asset': tracker.asset,
                'outcome': outcome,
                'qty_up': snap_qty_up,
                'qty_down': snap_qty_down,
                'pair_cost': snap_cost_up + snap_cost_dn,
                'total_spent': snap_cash_out,
                'sell_proceeds': sell_proceeds,
                'payout': resolution_payout,
                'net_payout': net_payout,
                'fees': fees_paid,
                'gross_pnl': gross_pnl,
                'pnl': pnl,
                'pnl_after_fees': pnl,
                'live_pnl': _live_net_pnl,
            })
            return
        
        try:
            # ── Price fetch: WS cache (instant) + HTTP fallback every 2s ─────
            _ws = self._ws_feed
            _ws_up_bid = _ws.get_bid(tracker.up_token_id) if _ws else None
            _ws_up_ask = _ws.get_ask(tracker.up_token_id) if _ws else None
            _ws_dn_bid = _ws.get_bid(tracker.down_token_id) if _ws else None
            _ws_dn_ask = _ws.get_ask(tracker.down_token_id) if _ws else None
            _ws_ok = bool(_ws_up_bid and _ws_up_ask and _ws_dn_bid and _ws_dn_ask)

            _now_t = time.time()
            _last_http = getattr(tracker, '_last_full_http', 0.0)
            _do_http = not _ws_ok or (_now_t - _last_http) >= 2.0

            fetch_latency_ms = 0.0
            fetch_start = _now_t
            up_book = {}
            down_book = {}

            if _do_http:
                async def fetch_book(token_id):
                    if not token_id:
                        return {}
                    url = f"{self.CLOB_API_URL}/book?token_id={token_id}"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=0.5)) as response:
                            if response.status == 200:
                                return await response.json()
                    except asyncio.TimeoutError:
                        pass
                    return {}
                up_book, down_book = await asyncio.gather(
                    fetch_book(tracker.up_token_id),
                    fetch_book(tracker.down_token_id)
                )
                fetch_latency_ms = (time.time() - fetch_start) * 1000
                tracker._last_full_http = time.time()
                tracker.up_orderbook = self._compress_orderbook(up_book)
                tracker.down_orderbook = self._compress_orderbook(down_book)
                tracker.orderbook_updated_at = time.time()
                asks_up   = up_book.get('asks', [])
                asks_down = down_book.get('asks', [])
                bids_up   = up_book.get('bids', [])
                bids_down = down_book.get('bids', [])
                if asks_up:   tracker.up_price      = min(float(a.get('price', 1.0)) for a in asks_up   if a.get('price'))
                if asks_down: tracker.down_price    = min(float(a.get('price', 1.0)) for a in asks_down if a.get('price'))
                if bids_up:   tracker.last_up_bid   = max(float(b.get('price', 0.0)) for b in bids_up   if b.get('price'))
                if bids_down: tracker.last_down_bid = max(float(b.get('price', 0.0)) for b in bids_down if b.get('price'))
            else:
                # Use WS book snapshots for display
                _wb_up = _ws.get_book(tracker.up_token_id)
                _wb_dn = _ws.get_book(tracker.down_token_id)
                if _wb_up: up_book = _wb_up
                if _wb_dn: down_book = _wb_dn

            # WS prices always override HTTP (they are the most current)
            if _ws_ok:
                tracker.up_price      = _ws_up_ask
                tracker.down_price    = _ws_dn_ask
                tracker.last_up_bid   = _ws_up_bid
                tracker.last_down_bid = _ws_dn_bid

            up_bid   = tracker.last_up_bid   or 0.0
            down_bid = tracker.last_down_bid or 0.0

            # Paper trading - calculate time to close for urgency
            if tracker.up_price and tracker.down_price and tracker.paper_trader.market_status == 'open':
                # Skip trading if paused
                if self.paused:
                    return

                # Global kill switch: stop all trading if total PnL <= -$50
                _global_pnl = self.cash_ref['balance'] - self.starting_balance
                if _global_pnl <= -50.0:
                    if not getattr(self, '_kill_switch_logged', False):
                        self._kill_switch_logged = True
                        print(f'[KILL SWITCH] Total PnL ${_global_pnl:+.2f} <= -$50.00 — ALL TRADING STOPPED')
                    return
                
                timestamp = datetime.now(timezone.utc).strftime('%H:%M:%S')
                
                # Calculate time remaining until market close
                time_to_close = None
                if tracker.window_end:
                    time_to_close = (tracker.window_end - now).total_seconds()
                
                # DEBUG: Print prices and strategy state every tick
                pt = tracker.paper_trader
                spread = abs(tracker.up_price - tracker.down_price) if tracker.up_price and tracker.down_price else 0
                ttc_str = f"{time_to_close:.0f}s" if time_to_close is not None else "N/A"
                extra = ""
                if pt.qty_up > 0 or pt.qty_down > 0:
                    ratio = pt.qty_up / pt.qty_down if pt.qty_down > 0 else (999 if pt.qty_up > 0 else 0)
                    locked = pt.calculate_locked_profit()
                    # Worst case = min of both resolution scenarios
                    if hasattr(pt, '_pnl_if'):
                        worst = min(pt._pnl_if('UP'), pt._pnl_if('DOWN'))
                    else:
                        worst = locked
                    extra = f" | ratio={ratio:.2f} locked=${locked:+.2f} worst=${worst:+.2f} trades={pt.trade_count}"
                # Add spot prediction info if available
                spot_info = ""
                if pt._spot_prediction and pt._spot_confidence > 0.55:
                    spot_delta = ""
                    if pt.trend_predictor.market_open_price and pt.trend_predictor.current_spot_price:
                        d = pt.trend_predictor.current_spot_price - pt.trend_predictor.market_open_price
                        ref_src = getattr(tracker, 'reference_price_source', '')
                        ref_tag = f" [{ref_src}]" if ref_src else ""
                        spot_delta = f" Δ${d:+,.0f}{ref_tag}"
                    spot_info = f" | 🎯{pt._spot_prediction} {pt._spot_confidence:.0%}{spot_delta}"
                print(f"🔍 [{tracker.asset}] UP=${tracker.up_price:.3f} DOWN=${tracker.down_price:.3f} | spread=${spread:.3f} | mode={pt.current_mode} | ttc={ttc_str}{extra}{spot_info} | {fetch_latency_ms:.0f}ms")
                
                # Execute one trade at a time: call strategy → execute live → reconcile → repeat.
                # This ensures every decision is based on confirmed state, not stale paper.
                _prev_realised = getattr(tracker.paper_trader, 'realised_pnl', None)
                trades = tracker.paper_trader.check_and_trade(
                    tracker.up_price,
                    tracker.down_price,
                    timestamp,
                    time_to_close=time_to_close,
                    up_bid=up_bid,
                    down_bid=down_bid,
                    up_orderbook=up_book,
                    down_orderbook=down_book
                )
                if trades:
                    _new_realised = getattr(tracker.paper_trader, 'realised_pnl', None)
                    _pnl_delta = ((_new_realised - _prev_realised)
                                  if _prev_realised is not None and _new_realised is not None
                                  else None)
                    _sell_count = sum(1 for t in trades
                                      if (len(t) >= 4 and t[0] == 'SELL'))
                    for trade in trades:
                        _trade_pnl = None  # per-trade PnL (from 5-tuple sells)
                        _live_fill_info = {}  # tracks live fill: {'filled':True/False, 'price','qty','cost'}
                        if len(trade) == 5:
                            action, side, actual_price, actual_qty, _trade_pnl = trade
                        elif len(trade) == 4:
                            action, side, actual_price, actual_qty = trade
                        else:
                            side, actual_price, actual_qty = trade
                            action = 'BUY'

                        # -- Mirror Mode: strategy now handles the flip internally --
                        # (LadderMateStrategy flips `leading` in check_and_trade)
                        _original_side = side
                        _mirror_active = getattr(self, 'mirror_mode', False)

                        # ── Forward trade to LiveExecutor for real execution ──
                        _executor = getattr(tracker, 'executor', None) or getattr(self, 'exec_sim', None)
                        # Gate: only execute live if armed AND this market opened after arm time
                        _live_blocked = (
                            _executor and getattr(_executor, 'live', False) and
                            (not self.live_armed or tracker.slug in self._pre_arm_markets)
                        )
                        if _live_blocked:
                            _executor = None  # fall through to paper-only path
                        if _executor and hasattr(_executor, 'simulate_buy'):
                            try:
                                _min_order_size = getattr(_executor, 'MIN_ORDER_SIZE', 5.0)
                                # Set correct token IDs for THIS tracker before executing
                                if hasattr(_executor, 'set_token_ids') and tracker.up_token_id and tracker.down_token_id:
                                    _executor.set_token_ids(tracker.up_token_id, tracker.down_token_id)
                                _ob = tracker.up_orderbook if side == 'UP' else tracker.down_orderbook
                                
                                if action == 'BUY':
                                    # Block BUY if we still have live shares on ANY side
                                    # (only for sell-based strategies like Laddermate, not Gaba)
                                    _is_hold_strategy = hasattr(pt, 'ORDER_SIZE')  # Gaba has ORDER_SIZE
                                    _live_up = tracker.live_inventory.get('UP', 0.0)
                                    _live_dn = tracker.live_inventory.get('DOWN', 0.0)
                                    if not _is_hold_strategy and (_live_up > 1.0 or _live_dn > 1.0):
                                        # Force sell orphan shares before allowing new BUY
                                        _orphan_side = 'UP' if _live_up > _live_dn else 'DOWN'
                                        _orphan_qty = tracker.live_inventory[_orphan_side]
                                        _bid_px = tracker.last_up_bid if _orphan_side == 'UP' else tracker.last_down_bid
                                        if _bid_px and _bid_px > 0.05:
                                            print(f"🔄 Force selling {_orphan_qty:.1f} orphan {_orphan_side} shares @ bid={_bid_px:.3f}")
                                            _orphan_paper = _executor._sim.simulate_fill(_orphan_side, _bid_px, _orphan_qty, {})
                                            _orphan_result = await _executor._place_order(
                                                'SELL', _orphan_side, _executor._get_token_id(_orphan_side),
                                                _bid_px, _orphan_qty, _bid_px * _orphan_qty, _orphan_paper,
                                                bid_price=_bid_px, stop_loss=True)
                                            if _orphan_result and _orphan_result.filled:
                                                tracker.live_inventory[_orphan_side] = max(0, tracker.live_inventory[_orphan_side] - _orphan_result.filled_qty)
                                                tracker.live_proceeds_total += getattr(_orphan_result, 'total_cost', 0)
                                                print(f"✅ Orphan {_orphan_side} sold: {_orphan_result.filled_qty:.1f} @ ${_orphan_result.fill_price:.3f} [inv: {tracker.live_inventory[_orphan_side]:.1f}]")
                                            else:
                                                print(f"⚠️ Orphan sell failed: {getattr(_orphan_result, 'reason', 'unknown')}")
                                        if hasattr(pt, 'reconcile_buy'):
                                            pt.reconcile_buy(side, actual_qty, actual_price, 0.0, 0.0)
                                        continue
                                    _result = await _executor.simulate_buy(
                                        side, actual_price, actual_qty, _ob,
                                        time_remaining_s=time_to_close)
                                    if _result and hasattr(_result, 'filled') and _result.filled:
                                        tracker.live_inventory[side] += _result.filled_qty
                                        print(f"🔥 LIVE BUY {side} filled: {_result.filled_qty:.1f} @ ${_result.fill_price:.3f}  [inv: {tracker.live_inventory[side]:.1f}]")
                                        _live_fill_info = {'filled': True, 'price': _result.fill_price, 'qty': _result.filled_qty, 'cost': getattr(_result, 'total_cost', _result.fill_price * _result.filled_qty)}
                                        tracker.live_cost_total += _live_fill_info['cost']
                                        # Reconcile paper state with actual fill
                                        if hasattr(pt, 'reconcile_buy'):
                                            pt.reconcile_buy(side, actual_qty, actual_price,
                                                             _result.filled_qty, _result.fill_price)
                                        # GTC SL will be posted after settlement confirms.
                                        # Store the SL price so settlement callback can post it.
                                        # Post maker TP sell after settlement
                                        _tp_price = None
                                        if hasattr(pt, '_rungs') and pt._rungs:
                                            for _r in reversed(pt._rungs):
                                                if _r['side'] == side:
                                                    _tp_price = _r.get('sell_target')
                                                    break
                                        if _tp_price and hasattr(_executor, '_deferred_gtc_sl'):
                                            _token_id = _executor._get_token_id(side) if hasattr(_executor, '_get_token_id') else None
                                            if _token_id:
                                                _executor._deferred_gtc_sl[_token_id] = {'side': side, 'price': _tp_price, 'qty': _result.filled_qty}
                                    elif _result:
                                        print(f"⚠️ LIVE BUY {side} NOT filled: {getattr(_result, 'reason', 'unknown')}")
                                        _live_fill_info = {'filled': False}
                                        # Order failed completely — reverse paper update
                                        if hasattr(pt, 'reconcile_buy'):
                                            pt.reconcile_buy(side, actual_qty, actual_price, 0.0, 0.0)
                                else:
                                    # SELL — use tracked inventory (filled BUY qty minus prior SELLs).
                                    # Do NOT use get_token_balance() — CLOB-bought tokens sit in
                                    # CLOB escrow and won't show as on-chain balance until withdrawn.
                                    _now = time.time()
                                    _backoff_until = getattr(tracker, '_sell_backoff_until', {}).get(side, 0.0)
                                    if _now < _backoff_until:
                                        wait = _backoff_until - _now
                                        print(f"⏳ LIVE SELL {side} skipped: backoff {wait:.0f}s after allowance failure")
                                        if hasattr(pt, 'reconcile_sell'):
                                            pt.reconcile_sell(side, actual_qty, actual_price,
                                                              _trade_pnl or 0.0, 0.0, 0.0,
                                                              fail_reason='SELL_BACKOFF',
                                                              min_order_size=_min_order_size)
                                        continue
                                    live_qty = tracker.live_inventory.get(side, 0.0)
                                    sell_qty = min(actual_qty, live_qty)
                                    if sell_qty < 0.5:
                                        print(f"⚠️ LIVE SELL {side} skipped: live inventory={live_qty:.1f} (paper wants {actual_qty:.1f})")
                                        # If stranded sweep already sold this qty, treat this sell
                                        # as already filled live so paper state is not restored.
                                        _sweep = getattr(tracker, '_recent_sweep_fill', {}).get(side, {})
                                        _sweep_qty = float(_sweep.get('qty', 0.0) or 0.0)
                                        _sweep_price = float(_sweep.get('price', actual_price) or actual_price)
                                        _sweep_age = time.time() - float(_sweep.get('ts', 0.0) or 0.0)
                                        if _sweep_qty >= (actual_qty - 0.001) and _sweep_age <= 180.0:
                                            tracker._recent_sweep_fill[side]['qty'] = max(0.0, _sweep_qty - actual_qty)
                                            print(f"ℹ️ LIVE SELL {side} matched against recent stranded sweep ({actual_qty:.1f} @ ${_sweep_price:.3f})")
                                            _live_fill_info = {'filled': True, 'price': _sweep_price, 'qty': actual_qty, 'cost': _sweep_price * actual_qty}
                                            tracker.live_proceeds_total += _live_fill_info['cost']
                                            if hasattr(pt, 'reconcile_sell'):
                                                pt.reconcile_sell(side, actual_qty, actual_price,
                                                                  _trade_pnl or 0.0,
                                                                  actual_qty, _sweep_price,
                                                                  min_order_size=_min_order_size)
                                        else:
                                            # Paper sold but live didn't — reconcile
                                            _live_fill_info = {'filled': False}
                                            if hasattr(pt, 'reconcile_sell'):
                                                pt.reconcile_sell(side, actual_qty, actual_price,
                                                                  _trade_pnl or 0.0, 0.0, 0.0,
                                                                  min_order_size=_min_order_size)
                                    else:
                                        _bid_px = up_bid if side == 'UP' else down_bid
                                        _is_stop_sell = (_trade_pnl is not None and _trade_pnl < 0)
                                        _result = await _executor.simulate_sell(side, actual_price, sell_qty, _ob, bid_price=_bid_px, stop_loss=_is_stop_sell)
                                        if _result and hasattr(_result, 'filled') and _result.filled:
                                            tracker.live_inventory[side] = max(0, tracker.live_inventory[side] - _result.filled_qty)
                                            print(f"🔥 LIVE SELL {side} filled: {_result.filled_qty:.1f} @ ${_result.fill_price:.3f}  [inv: {tracker.live_inventory[side]:.1f}]")
                                            _live_fill_info = {'filled': True, 'price': _result.fill_price, 'qty': _result.filled_qty, 'cost': getattr(_result, 'total_cost', _result.fill_price * _result.filled_qty)}
                                            tracker.live_proceeds_total += _live_fill_info['cost']
                                            # Reconcile: paper used actual_qty, live used sell_qty at fill_price
                                            if hasattr(pt, 'reconcile_sell'):
                                                pt.reconcile_sell(side, actual_qty, actual_price,
                                                                  _trade_pnl or 0.0,
                                                                  _result.filled_qty, _result.fill_price,
                                                                  fail_reason=getattr(_result, 'reason', ''),
                                                                  min_order_size=_min_order_size)
                                        elif _result:
                                            _r_reason = getattr(_result, 'reason', '')
                                            if 'SETTLEMENT_PENDING' in _r_reason:
                                                # Poller is actively confirming — skip silently, retry next tick
                                                _live_fill_info = {'filled': False}
                                                if hasattr(pt, 'reconcile_sell'):
                                                    pt.reconcile_sell(side, actual_qty, actual_price,
                                                                      _trade_pnl or 0.0, 0.0, 0.0,
                                                                      fail_reason=_r_reason,
                                                                      min_order_size=_min_order_size)
                                                continue
                                            print(f"⚠️ LIVE SELL {side} NOT filled: {_r_reason or 'unknown'}")
                                            _live_fill_info = {'filled': False}
                                            if 'NO_BALANCE_ALLOWANCE' in _r_reason:
                                                tracker._sell_backoff_until[side] = time.time() + 0.5
                                                print(f"⏸️  SELL {side} backoff 5s after allowance failure")
                                            # Sell failed — restore paper position
                                            if hasattr(pt, 'reconcile_sell'):
                                                pt.reconcile_sell(side, actual_qty, actual_price,
                                                                  _trade_pnl or 0.0, 0.0, 0.0,
                                                                  fail_reason=_r_reason,
                                                                  min_order_size=_min_order_size)
                            except Exception as _ex:
                                print(f"❌ LIVE {action} {side} error: {_ex}")
                                _live_fill_info = {'filled': False}
                        
                        pt = tracker.paper_trader
                        _is_live_mode = bool(_live_fill_info)  # non-empty dict = live mode active

                        # In live mode, if trade failed, stop processing this tick.
                        # Don't retry — wait for next tick with fresh prices.
                        if _is_live_mode and not _live_fill_info.get('filled', False):
                            print(f"⛔ [{tracker.asset.upper()}] {action} {side} NOT logged (live fill failed)")
                            continue

                        # Determine display values: use live fill data when available
                        if _is_live_mode and _live_fill_info.get('filled'):
                            _log_price = _live_fill_info['price']
                            _log_qty = _live_fill_info['qty']
                            _log_cost = _live_fill_info['cost']
                        else:
                            _log_price = actual_price
                            _log_qty = actual_qty
                            _log_cost = actual_price * actual_qty if action in ('BUY', 'SELL') else 0.0

                        # Track buy cost basis for live PnL calc
                        if action == 'BUY' and _is_live_mode and _live_fill_info.get('filled'):
                            if not hasattr(self, '_last_buy_cost_basis'):
                                self._last_buy_cost_basis = {}
                            self._last_buy_cost_basis[f'{tracker.slug}_{side}'] = _live_fill_info['price']
                        urgency_msg = (
                            f" [⚠️ {time_to_close:.0f}s left!]"
                            if time_to_close and time_to_close < URGENCY_THRESHOLD_SECONDS
                            else ""
                        )
                        _live_tag = " [LIVE]" if _is_live_mode else ""
                        print(f"📈 [{tracker.asset.upper()}] {action} {_log_qty:.1f} {side} @ ${_log_price:.3f} | Cost ${_log_cost:.2f}{_live_tag} | {fetch_latency_ms:.0f}ms{urgency_msg}")
                        
                        # Add to trade log
                        log_entry = {
                            'time': timestamp,
                            'asset': tracker.asset.upper(),
                            'market': tracker.slug,
                            'action': action,
                            'side': side,
                            'price': _log_price,
                            'qty': _log_qty,
                            'cost': _log_cost,
                            'pair_cost': pt.pair_cost
                        }
                        if _mirror_active:
                            # Show what the non-mirrored bot would have done
                            log_entry['mirror'] = True
                            log_entry['original_side'] = 'DOWN' if side == 'UP' else 'UP'
                            log_entry['original_price'] = tracker.down_price if side == 'UP' else tracker.up_price
                        if action == 'SELL':
                            if _is_live_mode and _live_fill_info.get('filled'):
                                # Use live fill data for PnL — paper PnL uses paper prices
                                _live_proceeds = _live_fill_info['cost']  # USDC received
                                _live_qty_sold = _live_fill_info['qty']
                                # Cost basis from tracker
                                _avg_cost = self._last_buy_cost_basis.get(f'{tracker.slug}_{side}', _log_price)
                                _live_cost_basis = _avg_cost * _live_qty_sold
                                log_entry['profit'] = round(_live_proceeds - _live_cost_basis, 4)
                            elif _trade_pnl is not None:
                                log_entry['profit'] = _trade_pnl
                            elif _pnl_delta is not None and _sell_count > 0:
                                log_entry['profit'] = _pnl_delta / _sell_count
                        self.trade_log.append(log_entry)
                        
                        # Keep only last 1000 trades
                        if len(self.trade_log) > 1000:
                            self.trade_log = self.trade_log[-1000:]

                # ── Stranded-share sweep ──────────────────────────────────────
                # Detect leftover inventory that didn't sell (rounding, partial fills)
                # and retry on every tick for >=MIN_ORDER_SIZE, or every 120s for smaller amounts.
                # ONLY run after market is resolved/closed — never during active trading!
                _executor = getattr(tracker, 'executor', None) or getattr(self, 'exec_sim', None)
                _pt_mode = getattr(pt, 'current_mode', '') if pt else ''
                _market_active = _pt_mode not in ('resolved', 'closed', '')
                # Only sweep stranded shares when live AND armed AND market opened after arm
                if (_executor and getattr(_executor, 'live', False) and not _market_active
                        and self.live_armed and tracker.slug not in self._pre_arm_markets):
                    # Ensure token IDs are set for this tracker
                    if (hasattr(_executor, 'set_token_ids')
                            and tracker.up_token_id and tracker.down_token_id):
                        _executor.set_token_ids(tracker.up_token_id, tracker.down_token_id)
                    _min_sell = getattr(_executor, 'MIN_ORDER_SIZE', 5.0)
                    if not hasattr(tracker, '_stranded_cooldown'):
                        tracker._stranded_cooldown = {}
                    _now_sweep = time.time()
                    for _sweep_side in ('UP', 'DOWN'):
                        _stranded = tracker.live_inventory.get(_sweep_side, 0.0)
                        _backoff_until = getattr(tracker, '_sell_backoff_until', {}).get(_sweep_side, 0.0)
                        if _now_sweep < _backoff_until:
                            wait = _backoff_until - _now_sweep
                            print(f"⏳ [STRANDED] {_sweep_side}: backoff {wait:.0f}s after allowance failure — skipping")
                            continue
                        if _stranded < 0.5:
                            continue
                        # Below min order size: only log every 120s to avoid spam
                        if _stranded < _min_sell:
                            _last = tracker._stranded_cooldown.get(_sweep_side, 0)
                            if _now_sweep - _last < 120.0:
                                continue
                            tracker._stranded_cooldown[_sweep_side] = _now_sweep
                            print(f"ℹ️ [STRANDED] {_sweep_side}: {_stranded:.2f} shares below min "
                                  f"({_min_sell}) — holding to resolution")
                            continue
                        # Enough to sell — determine current market price for this side
                        _sweep_price = (tracker.up_price if _sweep_side == 'UP'
                                        else tracker.down_price)
                        if not _sweep_price:
                            print(f"⚠️ [STRANDED] {_sweep_side}: {_stranded:.2f} shares — no price, skipping")
                            continue
                        print(f"🔄 [STRANDED SWEEP] {_sweep_side}: selling {_stranded:.2f} stranded shares @ ${_sweep_price:.3f}")
                        try:
                            _sweep_ob = (tracker.up_orderbook if _sweep_side == 'UP'
                                         else tracker.down_orderbook) or {}
                            _sweep_bid = up_bid if _sweep_side == 'UP' else down_bid
                            _sweep_result = await _executor.simulate_sell(
                                _sweep_side, _sweep_price, _stranded, _sweep_ob, bid_price=_sweep_bid)
                            if _sweep_result and getattr(_sweep_result, 'filled', False):
                                tracker.live_inventory[_sweep_side] = max(
                                    0.0, _stranded - _sweep_result.filled_qty)
                                _existing = tracker._recent_sweep_fill.get(_sweep_side, {'qty': 0.0, 'price': 0.0, 'ts': 0.0})
                                tracker._recent_sweep_fill[_sweep_side] = {
                                    'qty': float(_existing.get('qty', 0.0) or 0.0) + float(_sweep_result.filled_qty or 0.0),
                                    'price': float(_sweep_result.fill_price or _sweep_price),
                                    'ts': time.time(),
                                }
                                tracker._stranded_cooldown[_sweep_side] = 0  # reset cooldown on success
                                print(f"✅ [STRANDED SWEEP] {_sweep_side}: sold {_sweep_result.filled_qty:.2f} "
                                      f"@ ${_sweep_result.fill_price:.3f}  "
                                      f"[remaining: {tracker.live_inventory[_sweep_side]:.2f}]")
                            else:
                                _s_reason = getattr(_sweep_result, 'reason', 'unknown')
                                print(f"⚠️ [STRANDED SWEEP] {_sweep_side}: sell attempt failed "
                                      f"({_s_reason})")
                                if 'NO_BALANCE_ALLOWANCE' in _s_reason:
                                    tracker._sell_backoff_until[_sweep_side] = time.time() + 0.5
                                    print(f"⏸️  [STRANDED SWEEP] {_sweep_side} backoff 5s after allowance failure")
                        except Exception as _sweep_ex:
                            print(f"❌ [STRANDED SWEEP] {_sweep_side} error: {_sweep_ex}")

            tracker.last_update = time.time()
            
        except Exception as e:
            print(f"Error updating {tracker.slug}: {e}")
    
    async def check_resolution(self, session: aiohttp.ClientSession, tracker: MarketTracker):
        """Check if a market has been resolved"""
        pt = tracker.paper_trader
        
        # Already resolved, nothing to do
        if pt.market_status == 'resolved':
            return
            
        try:
            url = f"{self.GAMMA_API_URL}/events?slug={tracker.slug}"
            async with session.get(url) as response:
                if response.status == 200:
                    events = await response.json()
                    if events and len(events) > 0:
                        event = events[0]
                        markets = event.get('markets', [])
                        
                        for m in markets:
                            outcome = m.get('groupItemTitle', '').lower()
                            winner = m.get('winner')
                            
                            if winner:
                                if 'up' in outcome:
                                    resolution = 'UP'
                                elif 'down' in outcome:
                                    resolution = 'DOWN'
                                else:
                                    continue
                                
                                # Sync paper qty to actual live_inventory before computing payout
                                _inv_r2 = tracker.live_inventory
                                _live_up_r2 = _inv_r2.get('UP', 0.0)
                                _live_dn_r2 = _inv_r2.get('DOWN', 0.0)
                                _is_live_r2 = getattr(getattr(self, 'exec_sim', None), 'live', False)
                                if (_live_up_r2 > 0 or _live_dn_r2 > 0) and _is_live_r2:
                                    try:
                                        if abs(pt.qty_up - _live_up_r2) > 0.05 or abs(pt.qty_down - _live_dn_r2) > 0.05:
                                            print(f'[RESOLVE] Correcting paper->live qty: '
                                                  f'UP {pt.qty_up:.2f}->{_live_up_r2:.2f}  DOWN {pt.qty_down:.2f}->{_live_dn_r2:.2f}')
                                        if hasattr(pt, '_pos'):
                                            pt._pos['UP'].qty   = _live_up_r2
                                            pt._pos['DOWN'].qty = _live_dn_r2
                                        else:
                                            pt.qty_up   = _live_up_r2
                                            pt.qty_down = _live_dn_r2
                                    except Exception as _qe_r2:
                                        print(f'[RESOLVE] qty inject failed ({_qe_r2}) — using paper qty')

                                # Snapshot positions BEFORE resolve clears them
                                snap_qty_up   = pt.qty_up
                                snap_qty_down = pt.qty_down
                                snap_cost_up  = pt.cost_up
                                snap_cost_dn  = pt.cost_down
                                snap_cash_out = pt.cash_out
                                snap_sell_in  = pt.cash_in

                                pnl = pt.resolve_market(resolution)
                                fees_paid = getattr(pt, 'last_fees_paid', 0.0)
                                gross_pnl = getattr(pt, 'final_pnl_gross', pnl + fees_paid)
                                net_payout = max(0.0, pt.payout - fees_paid)
                                sell_proceeds = snap_sell_in
                                resolution_payout = pt.payout
                                _live_win_r2 = tracker.live_inventory.get(resolution, 0.0)
                                _live_net_r2 = round(_live_win_r2 + tracker.live_proceeds_total - tracker.live_cost_total, 4) if _is_live_r2 else None

                                print(f"\u26f3 [{tracker.asset.upper()}] Resolved: {resolution} | "
                                      f"Net: ${pnl:+.2f} | spent=${snap_cash_out:.2f} "
                                      f"sells=${sell_proceeds:.2f} payout=${resolution_payout:.2f} "
                                      f"(fees ${fees_paid:.2f})")

                                # Record outcome in trend predictor for future predictions
                                asset_spot = self.last_spot_prices.get(tracker.asset, self.last_btc_spot)
                                if tracker.spot_open_price and asset_spot:
                                    pt.trend_predictor.record_market_outcome(
                                        resolution, tracker.spot_open_price, asset_spot
                                    )

                                # Add to history
                                self.history.append({
                                    'resolved_at': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                                    'slug': tracker.slug,
                                    'asset': tracker.asset,
                                    'outcome': resolution,
                                    'qty_up': snap_qty_up,
                                    'qty_down': snap_qty_down,
                                    'pair_cost': snap_cost_up + snap_cost_dn,
                                    'total_spent': snap_cash_out,
                                    'sell_proceeds': sell_proceeds,
                                    'payout': resolution_payout,
                                    'net_payout': net_payout,
                                    'fees': fees_paid,
                                    'gross_pnl': gross_pnl,
                                    'pnl': pnl,
                                    'pnl_after_fees': pnl,
                                    'live_pnl': _live_net_r2,
                                })
                                return
                        
                        # No winner found yet - check if we've been waiting too long
                        now = datetime.now(timezone.utc)
                        if tracker.window_end:
                            time_since_close = (now - tracker.window_end).total_seconds()
                            # If we've waited more than 5 minutes without resolution, assume market failed
                            if time_since_close > 300 and pt.market_status != 'resolved':
                                # Liquidate at last known prices
                                liquidation_value = (pt.qty_up * tracker.last_up_bid) + (pt.qty_down * tracker.last_down_bid)
                                if liquidation_value == 0 and (pt.qty_up > 0 or pt.qty_down > 0):
                                    liquidation_value = min(pt.qty_up, pt.qty_down)
                                total_cost = pt.cost_up + pt.cost_down
                                fees_paid = pt.calculate_total_fees()
                                net_liquidation = max(0.0, liquidation_value - fees_paid)
                                pnl_after_fees = net_liquidation - total_cost
                                gross_pnl = liquidation_value - total_cost
                                
                                # Add net payout back to cash
                                self.cash_ref['balance'] += net_liquidation
                                
                                pt.market_status = 'resolved'
                                pt.resolution_outcome = 'TIMEOUT'
                                pt.payout = liquidation_value
                                pt.final_pnl = pnl_after_fees
                                pt.final_pnl_gross = gross_pnl
                                pt.last_fees_paid = fees_paid
                                
                                print(f"⚠️ [{tracker.asset.upper()}] Resolution timeout | Net: ${pnl_after_fees:+.2f} (fees ${fees_paid:.2f})")
                                
                                self.history.append({
                                    'resolved_at': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                                    'slug': tracker.slug,
                                    'asset': tracker.asset,
                                    'outcome': 'TIMEOUT',
                                    'qty_up': pt.qty_up,
                                    'qty_down': pt.qty_down,
                                    'pair_cost': pt.pair_cost,
                                    'total_spent': pt.cost_up + pt.cost_down,
                                    'payout': liquidation_value,
                                    'net_payout': net_liquidation,
                                    'fees': fees_paid,
                                    'gross_pnl': gross_pnl,
                                    'pnl': pnl_after_fees,
                                    'pnl_after_fees': pnl_after_fees
                                })
                                
        except Exception as e:
            print(f"Error checking resolution for {tracker.slug}: {e}")
    
    async def cleanup_old_markets(self):
        """Remove old resolved markets from active tracking"""
        to_remove = []
        for slug, tracker in self.active_markets.items():
            if tracker.paper_trader.market_status == 'resolved':
                # Keep resolved markets for 2 minutes so UI can show them
                if time.time() - tracker.last_update > 120:
                    to_remove.append(slug)
        
        for slug in to_remove:
            del self.active_markets[slug]
            print(f"🗑️ Removed old market: {slug}")
    
    async def broadcast(self, data: dict):
        """Broadcast data to all connected websockets"""
        if not self.websockets:
            return
        
        message = json.dumps(data)
        disconnected = set()
        
        for ws in list(self.websockets):  # snapshot — prevents RuntimeError if set changes during iteration
            try:
                await ws.send_str(message)
            except:
                disconnected.add(ws)
        
        self.websockets -= disconnected
    
    async def _update_and_check(self, session: aiohttp.ClientSession, tracker) -> None:
        """Update one market and check resolution — run concurrently via asyncio.gather."""
        await self.update_market(session, tracker)
        if tracker.window_end and datetime.now(timezone.utc) > tracker.window_end:
            if tracker.paper_trader.market_status != 'resolved':
                await self.check_resolution(session, tracker)

    async def _market_task_loop(self, session: aiohttp.ClientSession, slug: str) -> None:
        """Independent asyncio task per market — WS-event–driven, <1 µs detection latency."""
        _price_event = asyncio.Event()
        _events_registered = False

        while self.running and slug in self.active_markets:
            tracker = self.active_markets.get(slug)
            if tracker is None:
                break

            # Register once token IDs are available
            if not _events_registered and self._ws_feed and tracker.up_token_id and tracker.down_token_id:
                self._ws_feed.register_event(tracker.up_token_id, _price_event)
                self._ws_feed.register_event(tracker.down_token_id, _price_event)
                _events_registered = True

            try:
                await self._update_and_check(session, tracker)
            except Exception as _e:
                import logging as _log
                _log.getLogger(__name__).warning('[market-task %s] error: %s', slug, _e)

            # Block until WS price arrives (eliminates polling lag) — 1 ms hard cap
            _price_event.clear()
            if _events_registered:
                try:
                    await asyncio.wait_for(_price_event.wait(), timeout=0.001)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0.001)  # no WS yet — 1 ms fallback

    async def data_loop(self):
        """Main data loop"""
        connector = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            use_dns_cache=True,
            keepalive_timeout=60,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            # Start real-time WebSocket price feed
            from polymarket_ws import PolymarketWSFeed
            self._ws_feed = PolymarketWSFeed()
            await self._ws_feed.start(session)
            print("[WSFeed] real-time price feed started")
            while self.running:
                try:
                    # === FETCH SPOT PRICES FOR ALL ACTIVE ASSETS ===
                    # Each asset needs its own spot price for UP/DOWN prediction
                    try:
                        # Determine which assets have active markets
                        active_assets = set()
                        for tracker in self.active_markets.values():
                            if tracker.paper_trader.market_status == 'open':
                                active_assets.add(tracker.asset)
                        
                        # Always fetch BTC (it's the primary asset)
                        active_assets.add('btc')
                        
                        # Fetch spot prices for all active assets
                        now_utc = datetime.now(timezone.utc)
                        for asset in active_assets:
                            try:
                                spot_price = await fetch_asset_spot(session, asset)
                                if spot_price:
                                    self.last_spot_prices[asset] = spot_price
                                    if asset == 'btc':
                                        self.last_btc_spot = spot_price  # backward compat
                                    self.spot_fetch_errors = 0
                                    
                                    # Update strategies for this asset
                                    for tracker in self.active_markets.values():
                                        if tracker.asset == asset and tracker.paper_trader.market_status == 'open':
                                            # === REFERENCE PRICE LOGIC ===
                                            # Uses Chainlink oracle (on-chain) as primary source for BTC,
                                            # falling back to Binance kline if Chainlink unavailable.
                                            if tracker.reference_price is None:
                                                window_started = (tracker.event_start_time is None or 
                                                                 now_utc >= tracker.event_start_time)
                                                if window_started:
                                                    if tracker.event_start_time:
                                                        target_ts = tracker.event_start_time.timestamp()
                                                        ref_price = await fetch_asset_price_at_timestamp(session, asset, target_ts)
                                                        if ref_price:
                                                            tracker.reference_price = ref_price
                                                            # Source is set inside fetch_asset_price_at_timestamp log
                                                            tracker.reference_price_source = 'chainlink' if asset == 'btc' else 'binance_kline'
                                                            print(f"📍 [{asset.upper()}] Reference price: ${ref_price:,.2f} (Chainlink/kline at window start)")
                                                        else:
                                                            tracker.reference_price = spot_price
                                                            tracker.reference_price_source = 'spot_fallback'
                                                            print(f"📍 [{asset.upper()}] Reference price: ${spot_price:,.2f} (current spot fallback)")
                                                    else:
                                                        tracker.reference_price = spot_price
                                                        tracker.reference_price_source = 'first_spot'
                                                    
                                                    tracker.spot_open_price = tracker.reference_price
                                                    tracker.paper_trader.set_market_open_spot(tracker.reference_price)
                                            
                                            tracker.paper_trader.update_spot_price(spot_price)
                            except Exception as e:
                                if self.spot_fetch_errors <= 3:
                                    print(f"⚠️ {asset.upper()} spot fetch error: {e}")
                    except Exception as e:
                        self.spot_fetch_errors += 1
                        if self.spot_fetch_errors <= 3:
                            print(f"⚠️ Spot price fetch error: {e}")

                    # Discover new markets
                    await self.discover_markets(session)
                    
                    # Ensure each active market has its own independent asyncio task
                    for _slug in list(self.active_markets):
                        if _slug not in self._market_tasks or self._market_tasks[_slug].done():
                            self._market_tasks[_slug] = asyncio.create_task(
                                self._market_task_loop(session, _slug),
                                name=f'market-{_slug}'
                            )
                    # Cancel tasks for markets that were removed
                    for _slug in list(self._market_tasks):
                        if _slug not in self.active_markets:
                            self._market_tasks[_slug].cancel()
                            del self._market_tasks[_slug]
                    
                    # Cleanup old markets
                    await self.cleanup_old_markets()
                    
                    # Prepare broadcast data - only send NEWEST market per asset
                    active_data = {}
                    total_locked_profit = 0
                    total_position_value = 0
                    
                    # First, find the newest market per asset
                    newest_per_asset = {}
                    for slug, tracker in self.active_markets.items():
                        asset = tracker.asset
                        # Extract timestamp from slug
                        import re
                        match = re.search(r'-(\d+)$', slug)
                        timestamp = int(match.group(1)) if match else 0
                        
                        if asset not in newest_per_asset or timestamp > newest_per_asset[asset][1]:
                            newest_per_asset[asset] = (slug, timestamp)
                    
                    # Now only include newest markets in broadcast
                    newest_slugs = {slug for slug, _ in newest_per_asset.values()}
                    
                    _is_live = getattr(getattr(self, 'exec_sim', None), 'live', False)

                    for slug, tracker in self.active_markets.items():
                        pt = tracker.paper_trader
                        is_active = pt.market_status != 'resolved'
                        if is_active:
                            if _is_live:
                                # In live mode: use actual on-chain share counts from live_inventory
                                # guaranteed payout = min(qty_up, qty_dn) regardless of which side wins
                                inv_up = tracker.live_inventory.get('UP', 0.0)
                                inv_dn = tracker.live_inventory.get('DOWN', 0.0)
                                position_value = min(inv_up, inv_dn)
                            else:
                                # Paper mode: use paper trader quantities
                                min_qty = min(pt.qty_up, pt.qty_down)
                                fees_estimate = pt.calculate_total_fees()
                                position_value = max(0.0, min_qty - fees_estimate)
                            total_position_value += position_value
                            total_locked_profit += pt.locked_profit
                        
                        # Only include newest market per asset in UI data
                        if slug in newest_slugs:
                            _pt_state = tracker.paper_trader.get_state()
                            # Augment with live inventory so UI can show actual positions
                            _pt_state['live_qty_up'] = tracker.live_inventory.get('UP', 0.0)
                            _pt_state['live_qty_down'] = tracker.live_inventory.get('DOWN', 0.0)
                            _pt_state['is_live'] = _is_live
                            # Live PnL: cost spent vs proceeds received (real money)
                            _pt_state['live_cost_total'] = tracker.live_cost_total
                            _pt_state['live_proceeds_total'] = tracker.live_proceeds_total
                            _live_inv_up = tracker.live_inventory.get('UP', 0.0)
                            _live_inv_dn = tracker.live_inventory.get('DOWN', 0.0)
                            # Unrealised: value of held shares at current price
                            _up_val = _live_inv_up * (tracker.up_price or 0)
                            _dn_val = _live_inv_dn * (tracker.down_price or 0)
                            _pt_state['live_pnl_realtime'] = round(
                                tracker.live_proceeds_total + _up_val + _dn_val - tracker.live_cost_total, 4)
                            # Orderbook imbalance data for UI
                            _pt = tracker.paper_trader
                            _obk_data = {
                                'imb_up': getattr(_pt, '_obk_imb', {}).get('UP', 0.0),
                                'imb_down': getattr(_pt, '_obk_imb', {}).get('DOWN', 0.0),
                                'prev_imb_up': getattr(_pt, '_obk_prev_imb', {}).get('UP', 0.0),
                                'prev_imb_down': getattr(_pt, '_obk_prev_imb', {}).get('DOWN', 0.0),
                                'confirm_side': getattr(_pt, '_obk_confirm_side', None),
                                'confirm_count': getattr(_pt, '_obk_confirm_count', 0),
                                'sell_signal_up': getattr(_pt, '_obk_sell_signal', {}).get('UP', 0),
                                'sell_signal_down': getattr(_pt, '_obk_sell_signal', {}).get('DOWN', 0),
                                'chaos': getattr(_pt, '_chaos_mode', False),
                                'pending_sells': len(getattr(_pt, '_pending_sells', [])),
                            }
                            active_data[slug] = {
                                'asset': tracker.asset,
                                'up_price': tracker.up_price,
                                'down_price': tracker.down_price,
                                'window_time': f"{tracker.window_end.strftime('%H:%M:%S') if tracker.window_end else '--:--'}",
                                'paper_trader': _pt_state,
                                'obk': _obk_data,
                                'orderbooks': {
                                    'up': tracker.up_orderbook,
                                    'down': tracker.down_orderbook,
                                    'updated_at': tracker.orderbook_updated_at,
                                }
                            }
                    
                    # True balance = cash + value of locked positions
                    if _is_live:
                        # Live mode: starting_balance + realized live P&L + open position value
                        _live_realized = sum(
                            (h['live_pnl'] if h.get('live_pnl') is not None else h.get('pnl_after_fees', h['pnl']))
                            for h in self.history
                        )
                        true_balance = self.starting_balance + _live_realized + total_position_value
                    else:
                        true_balance = self.cash_ref['balance'] + total_position_value
                    
                    # Calculate W/D/L per asset
                    asset_wdl = {}
                    for asset in SUPPORTED_ASSETS:
                        asset_history = [h for h in self.history if h['asset'] == asset]
                        def _eff_pnl(h):
                            return h['live_pnl'] if h.get('live_pnl') is not None else h.get('pnl_after_fees', h['pnl'])
                        wins = sum(1 for h in asset_history if _eff_pnl(h) > 0)
                        draws = sum(1 for h in asset_history if _eff_pnl(h) == 0)
                        losses = sum(1 for h in asset_history if _eff_pnl(h) < 0)
                        total = len(asset_history)
                        total_pnl = sum(_eff_pnl(h) for h in asset_history)
                        realized_profit = sum(h.get('locked_profit', 0) for h in asset_history)
                        asset_wdl[asset] = {
                            'wins': wins,
                            'draws': draws,
                            'losses': losses,
                            'total': total,
                            'total_pnl': total_pnl,
                            'realized_profit': realized_profit,
                            'pnl_history': [_eff_pnl(h) for h in asset_history],
                        }
                    
                    # Use shared execution simulator stats (persists across all markets)
                    es = self.exec_sim.get_stats()
                    total_slippage_cost = es.get('total_slippage_cost', 0)

                    data = {
                        'starting_balance': self.starting_balance,
                        'current_balance': self.cash_ref['balance'],
                        'true_balance': true_balance,
                        'total_locked_profit': total_locked_profit,
                        'active_markets': active_data,
                        'history': self.history,
                        # Show full trade log across all markets
                        'trade_log': self.trade_log,
                        'paused': self.paused,
                        'live_armed': self.live_armed,
                        'pre_arm_count': len(self._pre_arm_markets),
                        'is_live': _is_live,
                        'asset_wdl': asset_wdl,
                        'supported_assets': SUPPORTED_ASSETS,
                        # Execution simulator stats (shared, never resets between markets)
                        'exec_stats': es
                    }
                    
                    await self.broadcast(data)
                    
                    self.update_count += 1
                    if self.update_count % 10 == 0:
                        total_pnl = true_balance - self.starting_balance
                        slip_str = f" | Slippage: -${total_slippage_cost:.4f}" if total_slippage_cost > 0 else ""
                        adj_pnl = total_pnl - total_slippage_cost
                        print(f"📊 Cash: ${self.cash_ref['balance']:.2f} | True Balance: ${true_balance:.2f} | Paper PnL: ${total_pnl:+.2f} | Real PnL (adj): ${adj_pnl:+.2f}{slip_str} | Active: {len(self.active_markets)}")
                    
                except Exception as e:
                    import traceback
                    print(f"Error in data loop: {e}")
                    traceback.print_exc()
                
                _tick_now = time.monotonic()
                _tick_prev = getattr(self, '_tick_last_at', None)
                if _tick_prev:
                    self._tick_interval_ms = round((_tick_now - _tick_prev) * 1000, 1)
                self._tick_last_at = _tick_now
                await asyncio.sleep(0.05)  # 50ms polling — low-latency price tracking
    
    async def index_handler(self, request):
        if not self._is_local_request(request) and _password_required():
            if not _is_session_valid(request.cookies.get('pairbot_sid', '')):
                raise web.HTTPFound('/login')
        resp = web.Response(text=HTML_TEMPLATE, content_type='text/html')
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    async def login_get_handler(self, request):
        if _is_session_valid(request.cookies.get('pairbot_sid', '')):
            raise web.HTTPFound('/')
        return web.Response(text=_make_login_html(), content_type='text/html')

    async def login_post_handler(self, request):
        data = await request.post()
        password = data.get('password', '')
        if _verify_dashboard_password(password):
            token = _create_session()
            resp = web.HTTPFound('/')
            resp.set_cookie('pairbot_sid', token, max_age=_SESSION_LIFETIME,
                            httponly=True, samesite='Lax')
            raise resp
        error = '<div class="err">Incorrect password.</div>'
        return web.Response(text=_make_login_html(error), content_type='text/html')

    async def logout_handler(self, request):
        token = request.cookies.get('pairbot_sid', '')
        _sessions.pop(token, None)
        resp = web.HTTPFound('/login')
        resp.del_cookie('pairbot_sid')
        raise resp

    async def diagnostics_handler(self, request):
        if not self._is_ws_authorized(request):
            return web.Response(status=403, text='Forbidden')
        result: dict = {
            'latency_gamma_ms': None, 'latency_clob_ms': None,
            'tick_interval_ms': getattr(self, '_tick_interval_ms', None),
            'ws_connections': len(self.websockets),
            'active_markets': len(self.active_markets),
            'ob_age_ms': None,
        }
        # Order book age
        now = time.time()
        ages = []
        for tracker in self.active_markets.values():
            ts = getattr(tracker, 'orderbook_updated_at', None)
            if ts:
                try:
                    ages.append((now - float(ts)) * 1000)
                except Exception:
                    pass
        if ages:
            result['ob_age_ms'] = round(max(ages), 1)
        # Measure external API latency
        endpoints = [
            ('latency_gamma_ms', 'https://gamma-api.polymarket.com/markets?limit=1'),
            ('latency_clob_ms',  'https://clob.polymarket.com/neg-risk-markets?limit=1'),
        ]
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for key, url in endpoints:
                t0 = time.monotonic()
                try:
                    async with sess.get(url, ssl=False) as r:
                        await r.read()
                    result[key] = round((time.monotonic() - t0) * 1000, 1)
                except Exception:
                    result[key] = None
        return web.json_response(result)

    async def settings_get_handler(self, request):
        if not self._is_ws_authorized(request):
            return web.Response(status=403, text='Forbidden')
        live = os.getenv('LIVE_TRADING', 'false').strip().lower() == 'true'
        key_field_map = {
            'api_key':        'POLY_API_KEY',
            'api_secret':     'POLY_API_SECRET',
            'api_passphrase': 'POLY_API_PASSPHRASE',
            'wallet_address': 'POLY_WALLET_ADDRESS',
            'private_key':    'POLY_PRIVATE_KEY',
        }
        def _mask(val):
            if not val:
                return ''
            if len(val) <= 8:
                return '*' * len(val)
            return val[:4] + '*' * (len(val) - 4)
        masked_keys = {
            field: _mask(os.getenv(env_key, '').strip())
            for field, env_key in key_field_map.items()
        }
        keys_configured = all(os.getenv(k, '').strip() for k in key_field_map.values())
        return web.json_response({
            'live_trading': live,
            'keys_configured': keys_configured,
            'keys': masked_keys,
            'active_assets': list(SUPPORTED_ASSETS),
            'max_loss_per_market': os.getenv('MAX_LOSS_PER_MARKET', '2.00'),
            'mirror_mode': getattr(self, 'mirror_mode', False),
        })

    async def settings_post_handler(self, request):
        if not self._is_ws_authorized(request):
            return web.Response(status=403, text='Forbidden')
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        live_trading = bool(body.get('live_trading', False))

        # Mirror mode toggle — propagate to all active strategies
        mirror_val = body.get('mirror_mode', None)
        if mirror_val is not None:
            self.mirror_mode = bool(mirror_val)
            for _t in self.active_markets.values():
                if hasattr(_t.paper_trader, 'mirror_mode'):
                    _t.paper_trader.mirror_mode = self.mirror_mode
            print(f'[Settings] Mirror mode: {"ON 🪞" if self.mirror_mode else "OFF"}')

        # Asset selection — update in-place so running loop picks it up immediately
        new_assets = [a for a in body.get('assets', []) if a in _ALL_ASSETS]
        if new_assets:
            SUPPORTED_ASSETS.clear()
            SUPPORTED_ASSETS.extend(new_assets)
            updates_assets_str = ','.join(new_assets)
            os.environ['ASSETS'] = updates_assets_str
            print(f'[Settings] Active assets: {SUPPORTED_ASSETS}')

        key_map = {
            'api_key':        'POLY_API_KEY',
            'api_secret':     'POLY_API_SECRET',
            'api_passphrase': 'POLY_API_PASSPHRASE',
            'wallet_address': 'POLY_WALLET_ADDRESS',
            'private_key':    'POLY_PRIVATE_KEY',
        }
        updates = {
            'LIVE_TRADING': 'true' if live_trading else 'false',
            'ASSETS': ','.join(SUPPORTED_ASSETS),
        }
        # Max loss per market
        max_loss_str = str(body.get('max_loss_per_market', '')).strip()
        if max_loss_str:
            try:
                max_loss_val = float(max_loss_str)
                updates['MAX_LOSS_PER_MARKET'] = str(max_loss_val)
                # Apply to all active strategy instances immediately
                for _t in self.active_markets.values():
                    if hasattr(_t.paper_trader, 'MAX_LOSS_PER_MARKET'):
                        _t.paper_trader.MAX_LOSS_PER_MARKET = max_loss_val
                print(f'[Settings] MAX_LOSS_PER_MARKET = ${max_loss_val:.2f}')
            except ValueError:
                pass
        # Password change
        new_password = str(body.get('new_password', '')).strip()
        if new_password:
            if len(new_password) < 8:
                return web.json_response({'error': 'Password must be at least 8 characters.'}, status=400)
            new_salt = _secrets.token_hex(16)
            new_hash = _pw_hash(new_password, new_salt)
            updates['DASHBOARD_PASSWORD_SALT'] = new_salt
            updates['DASHBOARD_PASSWORD_HASH'] = new_hash
            os.environ['DASHBOARD_PASSWORD_SALT'] = new_salt
            os.environ['DASHBOARD_PASSWORD_HASH'] = new_hash
            # Invalidate all existing sessions to force re-login
            _sessions.clear()
            print('[Settings] Password updated. All sessions invalidated.')
        for field, env_key in key_map.items():
            val = str(body.get(field, '')).strip()
            if val:
                updates[env_key] = val

        # Persist to .env beside web_bot_multi.py
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        existing = {}
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, _, v = line.partition('=')
                        existing[k.strip()] = v.strip()
        existing.update(updates)
        with open(env_path, 'w') as f:
            for k, v in existing.items():
                f.write(f'{k}={v}\n')

        # Apply to running process
        for k, v in updates.items():
            os.environ[k] = v

        # Re-initialize executor so new mode/keys take effect immediately
        try:
            if LiveExecutor is not None:
                self.exec_sim = LiveExecutor(latency_ms=25.0, max_slippage_pct=2.0)
                for _t in self.active_markets.values():
                    _t.executor = LiveExecutor(latency_ms=25.0, max_slippage_pct=2.0)
            else:
                self.exec_sim = ExecutionSimulator(latency_ms=25.0, max_slippage_pct=2.0)
                for _t in self.active_markets.values():
                    _t.executor = self.exec_sim
            mode = 'LIVE' if live_trading else 'PAPER'
            print(f'[Settings] Executor re-initialized in {mode} mode ({len(self.active_markets)} market executors updated).')
        except Exception as exc:
            return web.json_response(
                {'error': f'Settings saved but executor init failed: {exc}'}, status=500)

        return web.json_response(
            {'message': f'Saved. Mode: {"LIVE TRADING" if live_trading else "PAPER TRADING"}.'})

    def _is_local_request(self, request) -> bool:
        # When behind nginx, X-Real-IP carries the actual client IP
        real_ip = request.headers.get('X-Real-IP', '').strip()
        remote = real_ip if real_ip else (request.remote or '').strip()
        # If password is configured, no IP gets a free pass — require login
        if _password_required() and real_ip:
            return False
        return remote in ('127.0.0.1', '::1', 'localhost') or remote.startswith('::ffff:127.0.0.1')

    def _is_ws_authorized(self, request) -> bool:
        if self._is_local_request(request):
            return True
        # Session cookie (login page)
        if _is_session_valid(request.cookies.get('pairbot_sid', '')):
            return True
        # Legacy DASHBOARD_TOKEN query param
        token = os.getenv('DASHBOARD_TOKEN', '').strip()
        if token and request.query.get('token', '') == token:
            return True
        # If password is configured but no valid session → deny
        if _password_required():
            return False
        return True  # No auth configured = open
    
    async def websocket_handler(self, request):
        if not self._is_ws_authorized(request):
            return web.Response(status=403, text='Forbidden')

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        self.websockets.add(ws)
        print(f"WebSocket connected. Total: {len(self.websockets)}")
        
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        action = data.get('action')
                        
                        if action == 'pause':
                            self.paused = not self.paused
                            status = "PAUSED" if self.paused else "RESUMED"
                            print(f"🔄 Trading {status}")
                            await self.broadcast({'paused': self.paused})

                        elif action == 'arm_live':
                            if not self.live_armed:
                                # Snapshot all currently open markets — exclude them from live orders
                                self._pre_arm_markets = set(self.active_markets.keys())
                                self.live_armed = True
                                print(f"🟢 LIVE ARMED — pre-arm markets excluded: {self._pre_arm_markets}")
                            await self.broadcast({'live_armed': self.live_armed})

                        elif action == 'disarm_live':
                            self.live_armed = False
                            self._pre_arm_markets = set()
                            print("🔴 LIVE DISARMED")
                            await self.broadcast({'live_armed': self.live_armed})

                        elif action == 'start_current_market':
                            removed = self._pre_arm_markets & set(self.active_markets.keys())
                            self._pre_arm_markets -= removed
                            if removed:
                                print(f"🚀 START CURRENT MARKET — removed from pre-arm exclusion: {removed}")
                            await self.broadcast({'pre_arm_count': len(self._pre_arm_markets)})
                        
                        elif action == 'reset':
                            # Reset everything
                            self.starting_balance = self.initial_starting_balance
                            self.per_market_budget = self.initial_per_market_budget
                            self.cash_ref['balance'] = self.initial_starting_balance
                            self.history = []
                            self.trade_log = []
                            self.active_markets = {}
                            for _t in list(self._market_tasks.values()):
                                _t.cancel()
                            self._market_tasks.clear()
                            print(f"🔄 Bot RESET - Balance: ${self.starting_balance:.2f}")
                            await self.broadcast({
                                'starting_balance': self.starting_balance,
                                'current_balance': self.cash_ref['balance'],
                                'true_balance': self.starting_balance,
                                'total_locked_profit': 0,
                                'active_markets': {},
                                'history': [],
                                'trade_log': [],
                                'paused': self.paused
                            })
                    except Exception as e:
                        print(f"Error handling websocket message: {e}")
        finally:
            self.websockets.discard(ws)
            print(f"WebSocket disconnected. Total: {len(self.websockets)}")
        
        return ws
    
    # ── Workspace state persistence ─────────────────────────────────────────
    _WORKSPACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.workspace.json')

    async def workspace_get_handler(self, request):
        if not self._is_ws_authorized(request):
            return web.Response(status=403, text='Forbidden')
        try:
            if os.path.exists(self._WORKSPACE_PATH):
                with open(self._WORKSPACE_PATH, 'r') as f:
                    return web.json_response(json.loads(f.read()))
        except Exception:
            pass
        return web.json_response({'nodes': [], 'connections': [], 'nextId': 1})

    async def workspace_post_handler(self, request):
        if not self._is_ws_authorized(request):
            return web.Response(status=403, text='Forbidden')
        try:
            data = await request.json()
            with open(self._WORKSPACE_PATH, 'w') as f:
                f.write(json.dumps(data))
            return web.json_response({'ok': True})
        except Exception as e:
            return web.json_response({'ok': False, 'error': str(e)}, status=400)

    def create_app(self):
        app = web.Application()
        app.router.add_get('/', self.index_handler)
        app.router.add_get('/login', self.login_get_handler)
        app.router.add_post('/login', self.login_post_handler)
        app.router.add_get('/logout', self.logout_handler)
        app.router.add_get('/ws', self.websocket_handler)
        app.router.add_get('/api/settings', self.settings_get_handler)
        app.router.add_post('/api/settings', self.settings_post_handler)
        app.router.add_get('/api/diagnostics', self.diagnostics_handler)
        app.router.add_get('/api/workspace', self.workspace_get_handler)
        app.router.add_post('/api/workspace', self.workspace_post_handler)
        return app
    
    async def start(self):
        app = self.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        host = os.environ.get('HOST', '0.0.0.0').strip() or '0.0.0.0'
        try:
            port = int(os.environ.get('PORT', '8080'))
        except ValueError:
            port = 8080
        site = web.TCPSite(runner, host, port)
        await site.start()

        if host in ('127.0.0.1', 'localhost', '::1'):
            open_url = f"http://localhost:{port}"
        else:
            open_url = f"http://{host}:{port}"
        
        print("🤖 Multi-Market Bot starting...")
        print(f"📊 Tracking: {', '.join(a.upper() for a in SUPPORTED_ASSETS)}")
        print(f"🌐 Open {open_url} in your browser")
        if os.getenv('DASHBOARD_TOKEN', '').strip():
            print("🔐 Remote dashboard access requires ?token=<DASHBOARD_TOKEN>")
        print("Press Ctrl+C to stop\n")
        
        await self.data_loop()


if __name__ == '__main__':
    # Use uvloop if available — 2-4x faster asyncio event loop (C/libuv based)
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        print('[Boot] uvloop event loop active')
    except ImportError:
        print('[Boot] uvloop not available, using default asyncio')
    bot = MultiMarketBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
