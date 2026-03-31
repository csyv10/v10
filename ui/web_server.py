"""Aiohttp-based web dashboard for the Polymarket Dutch Book Bot.

Run via:  python main.py --web
Then open http://localhost:8090 in a browser.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from aiohttp import web

from config import ORDERBOOK_DEPTH, SPREAD_THRESHOLD

log = logging.getLogger(__name__)

# ─── Embedded HTML dashboard ─────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--surface:#161b22;--border:#30363d;
  --text:#c9d1d9;--muted:#8b949e;
  --green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;
}
body{background:var(--bg);color:var(--text);font-family:'Cascadia Code',Consolas,monospace;font-size:13px;overflow-y:auto}

/* ── Header ── */
#hdr{position:sticky;top:0;z-index:100;display:flex;align-items:center;padding:7px 14px;background:var(--surface);border-bottom:1px solid var(--border);gap:10px}
#hdr .name{font-weight:bold;color:var(--blue);letter-spacing:1px;font-size:12px}
#hdr .status{flex:1;text-align:center;font-weight:bold}
#hdr .pnl{min-width:130px;text-align:right;font-weight:bold;font-size:14px}

/* ── Orderbook section ── */
#ob-section{display:grid;grid-template-columns:210px 1fr 1fr;height:270px;gap:1px;background:var(--border)}
.panel{background:var(--surface);display:flex;flex-direction:column;overflow:hidden}
.ptitle{padding:5px 10px;font-weight:bold;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase;letter-spacing:.6px;flex-shrink:0}
#pnl-panel .ptitle{color:var(--blue)} #ob-up .ptitle{color:var(--green)} #ob-dn .ptitle{color:var(--red)}
#info{padding:8px 10px;overflow-y:auto;flex:1;font-size:12px}
.isect{margin-bottom:10px}
.isect-title{color:var(--blue);font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;padding-bottom:2px;border-bottom:1px solid var(--border)}
.irow{display:flex;justify-content:space-between;padding:1px 0}
.ikey{color:var(--muted)}.ival{font-weight:bold;text-align:right}
.price-bar{display:flex;justify-content:space-between;align-items:center;padding:5px 10px;border-bottom:1px solid var(--border);background:rgba(0,0,0,.2);flex-shrink:0}
.pb-bid{color:var(--green);font-weight:bold;font-size:14px}
.pb-ask{color:var(--red);font-weight:bold;font-size:14px}
.pb-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.pb-mid{color:var(--text);font-size:11px}
.ob-body{flex:1;overflow-y:auto;padding:2px 0}
.ob-row{display:grid;grid-template-columns:38px 1fr 1fr;padding:2px 10px;gap:4px;line-height:1.5}
.ob-row.best{font-weight:bold}
.ob-sep{border-top:1px solid var(--border);margin:3px 10px}
.ob-empty{padding:8px 10px;color:var(--muted)}
.ask{color:var(--red)}.bid{color:var(--green)}.ob-sz{text-align:right;color:var(--muted)}.mark{color:var(--yellow);margin-left:2px}

/* ── Stats section ── */
#stats-section{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);border-top:1px solid var(--border)}
.stat-box{background:var(--surface);padding:14px 16px}
.stat-label{font-size:9px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:6px}
.stat-value{font-size:26px;font-weight:bold;line-height:1.1}
.stat-sub{font-size:11px;color:var(--muted);margin-top:4px}
#worst-best{display:flex;justify-content:space-between;padding:6px 16px;background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border);font-size:12px;color:var(--muted)}

/* ── Charts ── */
.chart-box{background:var(--surface);padding:10px 14px 12px;border-top:1px solid var(--border)}
.chart-header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.chart-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}
.chart-subtitle{font-size:11px}
.chart-wrap{position:relative;height:130px}

/* ── Per-market stats grid ── */
#mkt-stats-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:2px 0;font-size:12px}
#mkt-stats-grid .mg-cell{padding:4px 8px;border-bottom:1px solid var(--border)}
#mkt-stats-grid .mg-head{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;padding:4px 8px 2px}
#mkt-stats-grid .mg-up{color:var(--green)} #mkt-stats-grid .mg-dn{color:var(--red)}
#mkt-stats-grid .mg-foot{grid-column:1/-1;padding:6px 8px 2px;border-top:1px solid var(--border);color:var(--muted);font-size:11px}

/* ── Trades ── */
#trades-box{background:var(--surface);border-top:1px solid var(--border)}
.box-header{display:flex;justify-content:space-between;align-items:center;padding:6px 12px;border-bottom:1px solid var(--border)}
.box-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}
.trade-row{display:flex;justify-content:space-between;align-items:center;padding:4px 12px;border-bottom:1px solid rgba(48,54,61,.4)}
.tr-side{font-weight:bold;min-width:70px}
.tr-price{font-weight:bold}
.tr-time{color:var(--muted);font-size:11px}

/* ── Log ── */
#log-panel{border-top:1px solid var(--border);background:var(--bg);display:flex;flex-direction:column;height:140px}
#log-panel .ptitle{color:var(--muted);flex-shrink:0}
#log-body{flex:1;overflow-y:auto;padding:4px 10px;font-size:11.5px}
.ll{padding:1px 0;white-space:nowrap;color:var(--muted)}.ll:last-child{color:var(--text)}

/* utils */
.green{color:var(--green)!important}.red{color:var(--red)!important}
.yellow{color:var(--yellow)!important}.blue{color:var(--blue)!important}
.bold{font-weight:bold}
.blink{animation:blink 1s step-start infinite}
@keyframes blink{50%{opacity:0}}
/* ── Tabs ── */
#tab-nav{display:flex;gap:2px;padding:0 14px;background:var(--surface);border-bottom:1px solid var(--border)}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);cursor:pointer;font:inherit;font-size:12px;letter-spacing:.5px;padding:8px 16px;text-transform:uppercase}
.tab-btn.active{border-bottom-color:var(--blue);color:var(--blue)}
.tab-btn:hover:not(.active){color:var(--text)}
#tab-settings{display:none;padding:20px;max-width:520px;margin:0 auto}
.s-section{margin-bottom:24px}
.s-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.s-row{margin-bottom:12px}
.s-label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}
.s-input{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);font:inherit;font-size:12px;padding:6px 10px;border-radius:4px;outline:none}
.s-input:focus{border-color:var(--blue)}
.s-select{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);font:inherit;font-size:12px;padding:6px 10px;border-radius:4px}
.mode-btns,.timing-btns{display:flex;gap:8px}
.toggle-btn{flex:1;background:var(--surface);border:1px solid var(--border);color:var(--muted);cursor:pointer;font:inherit;font-size:12px;padding:8px;border-radius:4px;text-align:center}
.toggle-btn.active{background:var(--blue);border-color:var(--blue);color:#0d1117;font-weight:bold}
.s-save{width:100%;background:var(--blue);border:none;color:#0d1117;cursor:pointer;font:inherit;font-size:13px;font-weight:bold;padding:10px;border-radius:4px;margin-top:8px}
.s-save:hover{opacity:.9}
.s-msg{margin-top:10px;font-size:12px;text-align:center;min-height:18px}
.s-msg.ok{color:var(--green)} .s-msg.err{color:var(--red)}
/* ── Header buttons ── */
.hdr-btn{background:none;border:1px solid var(--border);color:var(--muted);cursor:pointer;font:inherit;font-size:11px;letter-spacing:.3px;padding:3px 10px;border-radius:3px}
.hdr-btn:hover{border-color:var(--text);color:var(--text)}
.hdr-btn.pause-active{border-color:var(--yellow);color:var(--yellow)}

/* ── Mobile responsive ── */
@media(max-width:600px){
  /* Header: wrap items, bigger touch targets */
  #hdr{flex-wrap:wrap;padding:8px 10px;gap:8px}
  #hdr .name{font-size:11px;width:100%;text-align:center}
  #hdr .status{font-size:12px;order:1}
  #hdr .pnl{font-size:13px;min-width:auto;order:2}
  .hdr-btn{font-size:13px;padding:10px 18px;min-height:44px;min-width:44px;border-radius:6px;touch-action:manipulation}
  #h-paper{order:0}
  #ul-tabs{order:3;width:100%;justify-content:center}
  #ul-tabs .hdr-btn{flex:1;text-align:center}

  /* Tab navigation: bigger touch targets */
  #tab-nav{padding:0 8px;gap:0}
  .tab-btn{font-size:13px;padding:12px 16px;min-height:48px;flex:1;text-align:center;touch-action:manipulation}

  /* Orderbook section: stack vertically */
  #ob-section{grid-template-columns:1fr;height:auto}
  #pnl-panel{order:0}
  #ob-up{order:1;min-height:180px}
  #ob-dn{order:2;min-height:180px}
  #info{font-size:13px}
  .ob-row{grid-template-columns:42px 1fr 1fr;padding:4px 10px;min-height:32px;align-items:center}
  .price-bar{padding:8px 10px}

  /* Stats boxes: stack single column */
  #stats-section{grid-template-columns:1fr}
  .stat-box{padding:12px 16px}
  .stat-value{font-size:22px}

  /* Charts: taller on mobile */
  .chart-wrap{height:160px}
  .chart-box{padding:10px}

  /* Per-market stats: 3-col is ok but increase padding */
  #mkt-stats-grid{font-size:13px}
  #mkt-stats-grid .mg-cell{padding:6px 8px}

  /* Trades: bigger rows */
  .trade-row{padding:8px 12px;min-height:44px;align-items:center}
  .tr-side{font-size:13px}
  .tr-price{font-size:13px}

  /* Log */
  #log-panel{height:180px}
  #log-body{font-size:12px}
  .ll{padding:3px 0}

  /* Settings: bigger inputs and buttons */
  #tab-settings{padding:16px 12px;max-width:100%}
  .s-input,.s-select{font-size:16px;padding:10px 12px;min-height:44px;border-radius:6px}
  .toggle-btn{font-size:14px;padding:12px;min-height:48px;border-radius:6px;touch-action:manipulation}
  .s-save{font-size:15px;padding:14px;min-height:52px;border-radius:8px;touch-action:manipulation}
  .s-label{font-size:13px;margin-bottom:6px}
  .mode-btns,.timing-btns{gap:10px}

  /* General: prevent iOS zoom on focus */
  input,select,textarea{font-size:16px!important}

  /* Larger base font for readability */
  body{font-size:14px}
}
</style>
</head>
<body>

<!-- Header -->
<div id="hdr">
  <span class="name">▶ POLYMARKET DUTCH BOOK BOT</span>
  <span id="h-paper" style="display:none;background:var(--yellow);color:#0d1117;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;letter-spacing:1px">PAPER</span>
  <span id="ul-tabs" style="display:flex;gap:4px;margin-left:4px"></span>
  <span class="status" id="h-status">CONNECTING…</span>
  <span class="pnl" id="h-pnl">PnL: $0.00</span>
  <button class="hdr-btn" id="btn-pause" onclick="togglePause()">Pause</button>
  <button class="hdr-btn" onclick="doReset()">Reset</button>
</div>

<!-- Tab navigation -->
<div id="tab-nav">
  <button class="tab-btn active" onclick="switchTab('dashboard')">Dashboard</button>
  <button class="tab-btn" onclick="switchTab('settings')">&#9881; Settings</button>
</div>

<!-- Dashboard tab -->
<div id="tab-dashboard">

<!-- Orderbooks -->
<div id="ob-section">
  <div class="panel" id="pnl-panel">
    <div class="ptitle">Market &amp; Positions</div>
    <div id="info">—</div>
  </div>
  <div class="panel" id="ob-up">
    <div class="ptitle">UP orderbook</div>
    <div class="price-bar">
      <div><div class="pb-label">Bid</div><div class="pb-bid" id="pb-up-bid">—</div></div>
      <div style="text-align:center"><div class="pb-label">Mid</div><div class="pb-mid" id="pb-up-mid">—</div></div>
      <div style="text-align:right"><div class="pb-label">Ask</div><div class="pb-ask" id="pb-up-ask">—</div></div>
    </div>
    <div class="ob-body" id="ob-up-body"></div>
  </div>
  <div class="panel" id="ob-dn">
    <div class="ptitle">DOWN orderbook</div>
    <div class="price-bar">
      <div><div class="pb-label">Bid</div><div class="pb-bid" id="pb-dn-bid">—</div></div>
      <div style="text-align:center"><div class="pb-label">Mid</div><div class="pb-mid" id="pb-dn-mid">—</div></div>
      <div style="text-align:right"><div class="pb-label">Ask</div><div class="pb-ask" id="pb-dn-ask">—</div></div>
    </div>
    <div class="ob-body" id="ob-dn-body"></div>
  </div>
</div>

<!-- Stats boxes -->
<div id="stats-section">
  <div class="stat-box">
    <div class="stat-label">AVG SUM</div>
    <div class="stat-value" id="sv-avgsum">—</div>
    <div class="stat-sub" id="ss-avgsum">—</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">POSITION &Delta;</div>
    <div class="stat-value" id="sv-delta">—</div>
    <div class="stat-sub" id="ss-delta">—</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">&#8600; IF DOWN WINS</div>
    <div class="stat-value" id="sv-if-dn">—</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">&#8599; IF UP WINS</div>
    <div class="stat-value" id="sv-if-up">—</div>
  </div>
</div>
<div id="worst-best">
  <span>Worst: <strong id="wb-worst">—</strong></span>
  <span>Best: <strong id="wb-best">—</strong></span>
</div>

<!-- Per-market stats -->
<div class="chart-box" id="mkt-stats-box">
  <div class="chart-header">
    <span class="chart-title">CURRENT MARKET</span>
    <span class="chart-subtitle" id="mkt-stats-sub">—</span>
  </div>
  <div id="mkt-stats-grid"></div>
</div>

<!-- Market Prices chart -->
<div class="chart-box">
  <div class="chart-header">
    <span class="chart-title">MARKET PRICES</span>
    <span class="chart-subtitle" id="prices-sub"></span>
  </div>
  <div class="chart-wrap"><canvas id="chart-prices"></canvas></div>
</div>

<!-- Cumulative Shares chart -->
<div class="chart-box">
  <div class="chart-header">
    <span class="chart-title">CUMULATIVE SHARES</span>
    <span class="chart-subtitle" id="shares-sub"></span>
  </div>
  <div class="chart-wrap"><canvas id="chart-shares"></canvas></div>
</div>

<!-- Avg Trade Price chart -->
<div class="chart-box">
  <div class="chart-header">
    <span class="chart-title">AVG TRADE PRICE</span>
    <span class="chart-subtitle" id="avgprice-sub"></span>
  </div>
  <div class="chart-wrap"><canvas id="chart-avgprice"></canvas></div>
</div>

<!-- Recent Trades -->
<div id="trades-box">
  <div class="box-header">
    <span class="box-title">RECENT TRADES</span>
    <span id="trades-count" style="color:var(--muted);font-size:11px">0 total</span>
  </div>
  <div id="trades-body"><div style="padding:8px 12px;color:var(--muted)">No trades yet</div></div>
</div>

<!-- Log -->
<div id="log-panel">
  <div class="ptitle">Log</div>
  <div id="log-body"></div>
</div>

</div><!-- end tab-dashboard -->

<!-- Settings tab -->
<div id="tab-settings">
  <div class="s-section">
    <div class="s-title">Trading Mode</div>
    <div class="mode-btns">
      <div class="toggle-btn active" id="mode-paper" onclick="setMode('paper')">Paper Trading</div>
      <div class="toggle-btn" id="mode-live" onclick="setMode('live')">Live Trading</div>
    </div>
  </div>

  <div id="live-creds" style="display:none">
    <div class="s-section">
      <div class="s-title">Credentials</div>
      <div class="s-row">
        <label class="s-label">Polymarket Private Key (0x&hellip;)</label>
        <input class="s-input" type="password" id="s-privkey" placeholder="Leave blank to keep existing">
      </div>
      <div class="s-row">
        <label class="s-label">CLOB API Key <span style="color:var(--muted)">(optional &mdash; faster signing)</span></label>
        <input class="s-input" type="text" id="s-apikey" placeholder="Leave blank to keep existing">
      </div>
      <div class="s-row">
        <label class="s-label">CLOB Secret</label>
        <input class="s-input" type="password" id="s-secret" placeholder="Leave blank to keep existing">
      </div>
      <div class="s-row">
        <label class="s-label">CLOB Passphrase</label>
        <input class="s-input" type="password" id="s-passphrase" placeholder="Leave blank to keep existing">
      </div>
      <div class="s-row">
        <label class="s-label">Wallet Address <span style="color:var(--muted)">(proxy wallet, e.g. 0x33d70f3b&hellip;)</span></label>
        <input class="s-input" type="text" id="s-wallet" placeholder="Leave blank to keep existing">
      </div>
    </div>

    <div class="s-section">
      <div class="s-title">Start Timing</div>
      <div class="timing-btns">
        <div class="toggle-btn active" id="timing-now" onclick="setTiming('now')">Start Immediately</div>
        <div class="toggle-btn" id="timing-next" onclick="setTiming('next')">Wait for Next Market</div>
      </div>
    </div>
  </div>

  <div class="s-section">
    <div class="s-title">Underlying Asset</div>
    <select class="s-select" id="s-underlying">
      <option value="BTC">BTC</option>
      <option value="ETH">ETH</option>
      <option value="SOL">SOL</option>
    </select>
  </div>

  <button class="s-save" onclick="saveSettings()">Save &amp; Apply</button>
  <div class="s-msg" id="s-msg"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const pnlStr = v => (v >= 0 ? '+' : '') + '$' + v.toFixed(2);
const pnlCls = v => v >= 0 ? 'green' : 'red';
const timeCls = s => s < 30 ? 'red blink' : s < 60 ? 'yellow' : 'green';
const timeFmt = s => String(Math.floor(s/60)).padStart(2,'0') + ':' + String(Math.floor(s%60)).padStart(2,'0');
const irow = (k,v) => `<div class="irow"><span class="ikey">${k}</span><span class="ival">${v}</span></div>`;

// ── Chart.js setup ────────────────────────────────────────────────────────────
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';

const BASE_OPTS = {
  animation: false,
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: {display:true, position:'bottom', labels:{boxWidth:10, font:{size:10}, padding:8, color:'#8b949e'}},
    tooltip: {enabled:false}
  },
  scales: {
    x: {display:false},
    y: {grid:{color:'rgba(48,54,61,.6)'}, ticks:{font:{size:10}, maxTicksLimit:5, color:'#8b949e'}}
  }
};

function mkChart(id, datasets, yExtra={}) {
  return new Chart($(id), {
    type: 'line',
    data: {labels:[], datasets},
    options: {...BASE_OPTS, scales:{x:{display:false}, y:{...BASE_OPTS.scales.y, ...yExtra}}}
  });
}

const priceChart = mkChart('chart-prices', [
  {label:'UP Bid',  borderColor:'#3fb950', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'UP Ask',  borderColor:'rgba(63,185,80,.45)', data:[], pointRadius:0, borderWidth:1, tension:.2},
  {label:'DN Bid',  borderColor:'#f85149', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'DN Ask',  borderColor:'rgba(248,81,73,.45)', data:[], pointRadius:0, borderWidth:1, tension:.2},
], {min:0, max:1, ticks:{callback:v=>v.toFixed(2)}});

const sharesChart = mkChart('chart-shares', [
  {label:'UP', borderColor:'#3fb950', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'DN', borderColor:'#f85149', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
]);

const avgPriceChart = mkChart('chart-avgprice', [
  {label:'Avg UP', borderColor:'#3fb950', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'Avg DN', borderColor:'#f85149', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
], {min:0, max:1, ticks:{callback:v=>v.toFixed(2)}});

const MAX_PTS = 120;
let lastChartT = 0, currentSlug = null;

function chartPush(chart, lbl, ...vals) {
  chart.data.labels.push(lbl);
  chart.data.datasets.forEach((d,i) => d.data.push(vals[i] ?? null));
  if (chart.data.labels.length > MAX_PTS) {
    chart.data.labels.shift();
    chart.data.datasets.forEach(d => d.data.shift());
  }
  chart.update('none');
}

function resetCharts() {
  [priceChart, sharesChart, avgPriceChart].forEach(c => {
    c.data.labels = [];
    c.data.datasets.forEach(d => d.data = []);
    c.update('none');
  });
}

function updateCharts(s) {
  const now = Date.now();
  if (now - lastChartT < 1000) return;
  lastChartT = now;

  const slug = s.market?.slug ?? null;
  if (slug !== currentSlug) { currentSlug = slug; resetCharts(); }

  const t = new Date().toLocaleTimeString('no', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
  const upBid = s.orderbook_up?.bids[0]?.price ?? null;
  const upAsk = s.orderbook_up?.asks[0]?.price ?? null;
  const dnBid = s.orderbook_down?.bids[0]?.price ?? null;
  const dnAsk = s.orderbook_down?.asks[0]?.price ?? null;

  if (upBid !== null && upAsk !== null && dnBid !== null && dnAsk !== null)
    chartPush(priceChart, t, upBid, upAsk, dnBid, dnAsk);
  if (s.positions.up_shares > 0 || s.positions.down_shares > 0)
    chartPush(sharesChart, t, s.positions.up_shares, s.positions.down_shares);
  if (s.avg_up_price !== null || s.avg_down_price !== null)
    chartPush(avgPriceChart, t, s.avg_up_price, s.avg_down_price);
}

// ── Orderbook renderer ────────────────────────────────────────────────────────
function renderOB(bodyId, ob) {
  const el = $(bodyId);
  const side = bodyId.includes('up') ? 'up' : 'dn';
  if (!ob || (!ob.asks.length && !ob.bids.length)) {
    el.innerHTML = '<div class="ob-empty">Loading…</div>';
    [`pb-${side}-bid`,`pb-${side}-ask`,`pb-${side}-mid`].forEach(id => $(id).textContent='—');
    return;
  }
  const bestBid = ob.bids[0]?.price ?? null;
  const bestAsk = ob.asks[0]?.price ?? null;
  $(`pb-${side}-bid`).textContent = bestBid !== null ? bestBid.toFixed(4) : '—';
  $(`pb-${side}-ask`).textContent = bestAsk !== null ? bestAsk.toFixed(4) : '—';
  $(`pb-${side}-mid`).textContent = (bestBid !== null && bestAsk !== null) ? ((bestBid+bestAsk)/2).toFixed(4) : '—';
  let h = '';
  [...ob.asks].reverse().forEach((lvl,i,arr) => {
    const best = i===arr.length-1;
    h += `<div class="ob-row${best?' best':''}"><span class="ask">ASK</span><span>${lvl.price.toFixed(4)}${best?'<span class="mark">◄</span>':''}</span><span class="ob-sz">${lvl.size.toFixed(1)}</span></div>`;
  });
  h += '<div class="ob-sep"></div>';
  ob.bids.forEach((lvl,i) => {
    h += `<div class="ob-row${i===0?' best':''}"><span class="bid">BID</span><span>${lvl.price.toFixed(4)}${i===0?'<span class="mark">◄</span>':''}</span><span class="ob-sz">${lvl.size.toFixed(1)}</span></div>`;
  });
  el.innerHTML = h;
}

// ── Main state renderer ───────────────────────────────────────────────────────
function applyState(s) {
  // Underlying tabs
  if (s.underlyings) renderUlTabs(s.underlyings);

  // Header
  $('h-paper').style.display = s.paper ? 'inline' : 'none';
  let stHtml = esc(s.status);
  if (s.is_halted)            stHtml = `<span class="red bold">&#9888; HALTED</span>`;
  else if (s.status==='TRADING')  stHtml = `<span class="green bold">TRADING</span>`;
  else if (s.status==='SCANNING') stHtml = `<span class="yellow">SCANNING&#8230;</span>`;
  $('h-status').innerHTML = 'Status: ' + stHtml;
  const tot = s.pnl.total;
  $('h-pnl').innerHTML = `<span class="${pnlCls(tot)} bold">PnL: ${pnlStr(tot)}</span>`;
  if (s.is_paused !== _paused) {
    _paused = s.is_paused;
    const btn = document.getElementById('btn-pause');
    if (btn) { btn.textContent = _paused ? 'Resume' : 'Pause'; btn.classList.toggle('pause-active', _paused); }
  }

  // Info panel
  let info = '<div class="isect"><div class="isect-title">Market</div>';
  info += irow('Underlying', `<span class="blue bold">${esc(s.underlying)}</span>`);
  if (s.market) {
    const slug = s.market.slug.length > 22 ? s.market.slug.slice(0,22)+'…' : s.market.slug;
    info += irow('Slug', `<span title="${esc(s.market.slug)}">${esc(slug)}</span>`);
    info += irow('Time left', `<span class="${timeCls(s.market.seconds_left)} bold">${timeFmt(s.market.seconds_left)}</span>`);
    if (s.sum_ask !== null) {
      const dbCls = s.dutch_book_active ? 'green bold' : '';
      info += irow('Sum ask', `<span class="${dbCls}">${s.sum_ask.toFixed(4)}${s.dutch_book_active?' <span class="green">&#10003;</span>':''}</span>`);
    }
  } else {
    info += irow('Pair', '<span class="yellow">Scanning…</span>');
  }
  info += '</div><div class="isect"><div class="isect-title">PnL</div>';
  info += irow('Unrealised',     `<span class="${pnlCls(s.pnl.unrealised)} bold">${pnlStr(s.pnl.unrealised)}</span>`);
  info += irow('Realised',      `<span class="${pnlCls(s.pnl.realised)} bold">${pnlStr(s.pnl.realised)}</span>`);
  info += irow('Session total', `<span class="${pnlCls(s.pnl.total)} bold">${pnlStr(s.pnl.total)}</span>`);
  info += irow('Peak spent',    `<span class="yellow bold">$${(s.pnl.peak_cost||0).toFixed(2)}</span>`);
  info += irow('Trades',        `<span class="bold">${s.pnl.trade_count}</span>`);
  info += irow('Markets',       `<span class="bold">${s.pnl.markets_traded}</span>`);
  info += '</div>';
  $('info').innerHTML = info;

  // Orderbooks
  renderOB('ob-up-body', s.orderbook_up);
  renderOB('ob-dn-body', s.orderbook_down);

  // Stats — AVG SUM
  const pos = s.positions;
  if (pos.avg_sum !== null) {
    const profit = (1 - pos.avg_sum) * 100;
    const cls = pos.avg_sum < 1 ? 'green' : 'red';
    $('sv-avgsum').innerHTML = `<span class="${cls}">${pos.avg_sum.toFixed(4)}</span>`;
    $('ss-avgsum').innerHTML = `<span class="${cls}">${profit >= 0?'+':''}${profit.toFixed(2)}% profit</span>`;
  } else {
    $('sv-avgsum').textContent = '—'; $('ss-avgsum').textContent = '—';
  }

  // Stats — POSITION Δ
  const diff = s.share_diff ?? 0;
  const dCls = pos.share_delta_pct > 3 ? 'red' : 'green';
  $('sv-delta').innerHTML = `<span class="${dCls}">${pos.share_delta_pct.toFixed(1)}%</span>`;
  const diffLabel = diff > 0 ? `+${diff} UP` : diff < 0 ? `${diff} DOWN` : 'balanced';
  $('ss-delta').innerHTML = `<span class="${dCls}">${diffLabel}</span>`;

  // Stats — Scenarios
  const sc = s.scenarios;
  if (sc) {
    $('sv-if-dn').innerHTML = `<span class="${pnlCls(sc.down_wins)}">${pnlStr(sc.down_wins)}</span>`;
    $('sv-if-up').innerHTML = `<span class="${pnlCls(sc.up_wins)}">${pnlStr(sc.up_wins)}</span>`;
    $('wb-worst').innerHTML = `<span class="red">${pnlStr(sc.worst)}</span>`;
    $('wb-best').innerHTML  = `<span class="green">${pnlStr(sc.best)}</span>`;
  }

  // Per-market stats box
  const ms = s.mkt_stats;
  if (ms) {
    const fmt4 = v => v != null ? v.toFixed(4) : '—';
    const fmtd = v => v != null ? '$' + v.toFixed(4) : '—';
    const hasPos = ms.up_shares > 0 || ms.down_shares > 0;
    $('mkt-stats-sub').innerHTML = hasPos
      ? `${ms.fills} fill${ms.fills!==1?'s':''} &mdash; total spent: <span class="yellow bold">$${ms.total_spent.toFixed(4)}</span>`
      : 'no position this market';
    let g = '';
    g += `<div class="mg-head"></div><div class="mg-head mg-up">UP</div><div class="mg-head mg-dn">DOWN</div>`;
    g += `<div class="mg-cell">Shares</div><div class="mg-cell mg-up">${ms.up_shares}</div><div class="mg-cell mg-dn">${ms.down_shares}</div>`;
    g += `<div class="mg-cell">Cost</div><div class="mg-cell mg-up">${fmtd(ms.up_cost)}</div><div class="mg-cell mg-dn">${fmtd(ms.down_cost)}</div>`;
    g += `<div class="mg-cell">Avg price</div><div class="mg-cell mg-up">${fmt4(ms.avg_up)}</div><div class="mg-cell mg-dn">${fmt4(ms.avg_down)}</div>`;
    const sumCls = ms.avg_sum != null ? (ms.avg_sum < 1 ? 'green' : 'red') : '';
    const sumStr = ms.avg_sum != null ? `<span class="${sumCls}">${ms.avg_sum.toFixed(4)}</span>` : '—';
    g += `<div class="mg-foot">Avg sum: ${sumStr} &nbsp;|&nbsp; Total spent: <span class="yellow">$${ms.total_spent.toFixed(4)}</span> &nbsp;|&nbsp; Fills: ${ms.fills}</div>`;
    $('mkt-stats-grid').innerHTML = g;
  }

  // Chart subtitles
  const upBid = s.orderbook_up?.bids[0]?.price, upAsk = s.orderbook_up?.asks[0]?.price;
  const dnBid = s.orderbook_down?.bids[0]?.price, dnAsk = s.orderbook_down?.asks[0]?.price;
  if (upBid && upAsk && dnBid && dnAsk) {
    $('prices-sub').innerHTML =
      `<span class="green">UP ${upBid.toFixed(2)} / ${upAsk.toFixed(2)}</span>` +
      ` &mdash; <span class="red">DN ${dnBid.toFixed(2)} / ${dnAsk.toFixed(2)}</span>`;
  }
  if (pos.up_shares > 0 || pos.down_shares > 0) {
    const uA = s.avg_up_price ? s.avg_up_price.toFixed(4) : '—';
    const dA = s.avg_down_price ? s.avg_down_price.toFixed(4) : '—';
    $('shares-sub').innerHTML =
      `<span class="green">UP ${pos.up_shares} @${uA}</span>` +
      ` &mdash; <span class="red">DN ${pos.down_shares} @${dA}</span>`;
    if (s.avg_up_price || s.avg_down_price) {
      $('avgprice-sub').innerHTML =
        `<span class="green">${uA}</span> &mdash; <span class="red">${dA}</span>`;
    }
  }

  // Recent trades
  $('trades-count').textContent = `${s.pnl.trade_count} total`;
  const trades = s.recent_trades || [];
  if (trades.length) {
    $('trades-body').innerHTML = trades.map(t => {
      const cls = t.side === 'UP' ? 'green' : 'red';
      return `<div class="trade-row">
        <span class="tr-side ${cls}">${t.side} &times;${t.shares}</span>
        <span class="tr-price">@ ${t.price.toFixed(4)}</span>
        <span class="tr-time">${esc(t.time)}</span>
      </div>`;
    }).join('');
  } else {
    $('trades-body').innerHTML = '<div style="padding:8px 12px;color:var(--muted)">No trades yet</div>';
  }

  // Charts
  updateCharts(s);

  // Log
  const logEl = $('log-body');
  const atBot = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 5;
  logEl.innerHTML = s.logs.map(l => `<div class="ll">${esc(l)}</div>`).join('');
  if (atBot) logEl.scrollTop = logEl.scrollHeight;
}

// ── Tab switching ──────────────────────────────────────────────────────────────
function switchTab(name) {
  document.getElementById('tab-dashboard').style.display = name === 'dashboard' ? '' : 'none';
  document.getElementById('tab-settings').style.display  = name === 'settings'  ? 'block' : 'none';
  document.querySelectorAll('.tab-btn').forEach((b,i) => {
    b.classList.toggle('active', (i===0 && name==='dashboard') || (i===1 && name==='settings'));
  });
  if (name === 'settings') loadSettings();
}

// ── Settings form ──────────────────────────────────────────────────────────────
let settingsMode = 'paper';
let settingsTiming = 'now';

function setMode(m) {
  settingsMode = m;
  document.getElementById('mode-paper').classList.toggle('active', m === 'paper');
  document.getElementById('mode-live').classList.toggle('active', m === 'live');
  document.getElementById('live-creds').style.display = m === 'live' ? '' : 'none';
}

function setTiming(t) {
  settingsTiming = t;
  document.getElementById('timing-now').classList.toggle('active', t === 'now');
  document.getElementById('timing-next').classList.toggle('active', t === 'next');
}

async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    if (!r.ok) return;
    const s = await r.json();
    setMode(s.paper ? 'paper' : 'live');
    const sel = document.getElementById('s-underlying');
    if (sel) sel.value = s.underlying || 'BTC';
    const msg = document.getElementById('s-msg');
    if (msg && s.has_private_key) {
      msg.textContent = 'Private key is set.';
      msg.className = 's-msg ok';
    }
  } catch {}
}

async function saveSettings() {
  const msg = document.getElementById('s-msg');
  msg.textContent = 'Saving\u2026'; msg.className = 's-msg';
  try {
    const body = {
      paper:             settingsMode === 'paper',
      underlying:        document.getElementById('s-underlying').value,
      start_next_market: settingsTiming === 'next',
      private_key:       document.getElementById('s-privkey')?.value   || '',
      clob_api_key:      document.getElementById('s-apikey')?.value     || '',
      clob_secret:       document.getElementById('s-secret')?.value     || '',
      clob_passphrase:   document.getElementById('s-passphrase')?.value || '',
      wallet_address:    document.getElementById('s-wallet')?.value     || '',
    };
    const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    if (d.ok) {
      msg.textContent = d.message || 'Saved!'; msg.className = 's-msg ok';
      // Clear sensitive fields
      ['s-privkey','s-secret','s-passphrase','s-wallet'].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
    } else {
      msg.textContent = d.error || 'Error'; msg.className = 's-msg err';
    }
  } catch(e) {
    msg.textContent = 'Network error'; msg.className = 's-msg err';
  }
}

// ── Pause / Reset ──────────────────────────────────────────────────────────────
let _paused = false;

async function togglePause() {
  const ep = _paused ? '/api/resume' : '/api/pause';
  await fetch(ep, {method:'POST'});
  _paused = !_paused;
  const btn = document.getElementById('btn-pause');
  btn.textContent = _paused ? 'Resume' : 'Pause';
  btn.classList.toggle('pause-active', _paused);
}

async function doReset() {
  if (!confirm('Reset all statistics? This cannot be undone.')) return;
  await fetch('/api/reset', {method:'POST'});
}

// ── Underlying selector ───────────────────────────────────────────────────────
let activeUnderlying = null;
let _knownUnderlyings = [];

function setUnderlying(u) {
  activeUnderlying = u;
  document.querySelectorAll('.ul-btn').forEach(b => {
    const on = b.dataset.u === u;
    b.style.borderColor = on ? 'var(--blue)' : '';
    b.style.color = on ? 'var(--blue)' : '';
  });
  resetCharts();
}

function renderUlTabs(underlyings) {
  if (underlyings.length <= 1) return;
  const el = $('ul-tabs');
  const same = _knownUnderlyings.length === underlyings.length &&
               _knownUnderlyings.every((u,i) => u === underlyings[i]);
  if (same) return;
  _knownUnderlyings = underlyings;
  if (!activeUnderlying) activeUnderlying = underlyings[0];
  el.innerHTML = underlyings.map(u =>
    `<button class="hdr-btn ul-btn" data-u="${u}" onclick="setUnderlying('${u}')">${u}</button>`
  ).join('');
  setUnderlying(activeUnderlying);
}

// ── Poll ──────────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const u = activeUnderlying ? '?u=' + activeUnderlying : '';
    const r = await fetch('/api/state' + u);
    if (r.ok) applyState(await r.json());
    else $('h-status').innerHTML = `<span class="yellow">HTTP ${r.status}</span>`;
  } catch {
    $('h-status').innerHTML = '<span class="red">DISCONNECTED</span>';
  }
}
poll();
setInterval(poll, 100);
</script>
</body>
</html>
"""


# ─── State serialiser ─────────────────────────────────────────────────────────

def _ob_levels(ob, side: str, depth: int) -> list:
    if ob is None:
        return []
    levels = ob.top_asks(depth) if side == "ask" else ob.top_bids(depth)
    return [{"price": lvl.price, "size": lvl.size} for lvl in levels]


def _get_engine(request: web.Request):
    """Return the engine matching the ?u= query param, or the first engine."""
    u = request.rel_url.query.get("u", "").upper()
    engines = request.app["engines"]
    return engines.get(u) or next(iter(engines.values()))


def build_state(engine, underlyings: list = None) -> dict:
    pair   = engine._current_pair
    pos    = engine.tracker.position
    ob_up  = engine.ob_up
    ob_dn  = engine.ob_down

    sum_ask = None
    dutch_book = False
    if ob_up and ob_dn:
        a_up = ob_up.best_ask
        a_dn = ob_dn.best_ask
        if a_up is not None and a_dn is not None:
            sum_ask = a_up + a_dn
            dutch_book = sum_ask < (1.0 - SPREAD_THRESHOLD)

    market = None
    if pair is not None:
        ms_left = pair.ms_until_end
        market = {
            "slug":         pair.short_label(),
            "up_token":     pair.up_token_id[:16] + "…",
            "down_token":   pair.down_token_id[:16] + "…",
            "seconds_left": max(0.0, ms_left / 1000),
            "tradeable":    pair.is_tradeable,
        }

    up_wins = pos.up_shares * 1.0 - pos.total_cost
    dn_wins = pos.down_shares * 1.0 - pos.total_cost

    return {
        "status":     engine.status,
        "underlying": engine.underlying,
        "is_halted":  engine.risk.is_halted,
        "is_paused":  engine.risk.is_halted,
        "halt_reason": engine.risk.halt_reason,
        "pnl": {
            "total":          engine.tracker.total_pnl,
            "realised":       engine.tracker.realised_pnl,
            "unrealised":     engine.tracker.unrealised_pnl,
            "trade_count":    engine.tracker.trade_count,
            "peak_cost":      engine.tracker.peak_cost,
            "markets_traded": engine.tracker.markets_traded,
        },
        "mkt_stats": {
            "up_shares":    pos.up_shares,
            "down_shares":  pos.down_shares,
            "up_cost":      round(pos.up_cost, 4),
            "down_cost":    round(pos.down_cost, 4),
            "avg_up":       round(pos.avg_up_price, 4) if pos.avg_up_price else None,
            "avg_down":     round(pos.avg_down_price, 4) if pos.avg_down_price else None,
            "avg_sum":      round(pos.avg_sum, 4) if pos.avg_sum else None,
            "total_spent":  round(pos.total_cost, 4),
            "fills":        len(pos.fills),
        },
        "market": market,
        "positions": {
            "up_shares":       pos.up_shares,
            "down_shares":     pos.down_shares,
            "share_delta_pct": pos.share_delta_pct * 100,
            "avg_sum":         pos.avg_sum,
        },
        "share_diff":     pos.up_shares - pos.down_shares,
        "avg_up_price":   pos.avg_up_price,
        "avg_down_price": pos.avg_down_price,
        "scenarios": {
            "up_wins":   round(up_wins, 4),
            "down_wins": round(dn_wins, 4),
            "worst":     round(min(up_wins, dn_wins), 4),
            "best":      round(max(up_wins, dn_wins), 4),
        },
        "recent_trades": [
            {"side": f.side, "shares": f.shares, "price": f.price, "time": f.ts}
            for f in reversed(pos.fills[-25:])
        ],
        "sum_ask":           sum_ask,
        "dutch_book_active": dutch_book,
        "orderbook_up": {
            "asks": _ob_levels(ob_up, "ask", ORDERBOOK_DEPTH),
            "bids": _ob_levels(ob_up, "bid", ORDERBOOK_DEPTH),
        },
        "orderbook_down": {
            "asks": _ob_levels(ob_dn, "ask", ORDERBOOK_DEPTH),
            "bids": _ob_levels(ob_dn, "bid", ORDERBOOK_DEPTH),
        },
        "logs":  list(engine.log_lines)[-60:],
        "paper": engine.paper,
        "underlyings": underlyings or [engine.underlying],
    }


# ─── .env helpers ─────────────────────────────────────────────────────────────

_ENV_PATH = Path(__file__).parent.parent / ".env"


def _read_env() -> dict:
    """Read current .env file into a dict."""
    env = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(values: dict) -> None:
    """Write dict to .env file (only updates provided keys, keeps rest)."""
    env = _read_env()
    env.update(values)
    lines = []
    for k, v in env.items():
        lines.append(f"{k}={v}")
    _ENV_PATH.write_text("\n".join(lines) + "\n")


async def _restart_engine(app: web.Application, paper: bool, underlying: str) -> None:
    """Stop the engine for `underlying` and start a fresh one."""
    old = app["engines"].get(underlying)
    if old:
        old.stop()
        await asyncio.sleep(1.5)

    from bot.bot_engine import BotEngine
    engine = BotEngine(underlying=underlying, paper=paper)
    app["engines"][underlying] = engine
    asyncio.create_task(engine.run())


# ─── Request handlers ─────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def handle_state(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    underlyings = list(request.app["engines"].keys())
    state = build_state(engine, underlyings)
    return web.json_response(state)


async def handle_pause(request: web.Request) -> web.Response:
    _get_engine(request).pause()
    return web.json_response({"ok": True})


async def handle_resume(request: web.Request) -> web.Response:
    _get_engine(request).resume()
    return web.json_response({"ok": True})


async def handle_reset(request: web.Request) -> web.Response:
    _get_engine(request).reset_stats()
    return web.json_response({"ok": True})


async def handle_settings_get(request: web.Request) -> web.Response:
    engine = _get_engine(request)
    env = _read_env()
    return web.json_response({
        "paper":           engine.paper,
        "underlying":      engine.underlying,
        "has_private_key": bool(env.get("PRIVATE_KEY", "").strip()),
        "has_clob_creds":  bool(env.get("CLOB_API_KEY", "").strip()),
        "is_paused":       engine.risk.is_halted,
    })


async def handle_settings_post(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    paper      = bool(data.get("paper", True))
    underlying = str(data.get("underlying", "BTC")).upper()
    skip_next  = bool(data.get("start_next_market", False))

    # Update .env with any provided credentials (empty string = keep existing)
    env_updates = {}
    for field, key in [
        ("private_key",     "PRIVATE_KEY"),
        ("clob_api_key",    "CLOB_API_KEY"),
        ("clob_secret",     "CLOB_SECRET"),
        ("clob_passphrase", "CLOB_PASSPHRASE"),
        ("wallet_address",  "WALLET_ADDRESS"),
    ]:
        val = str(data.get(field, "")).strip()
        if val:
            env_updates[key] = val
    env_updates["UNDERLYING"] = underlying
    env_updates["PAPER_MODE"] = "true" if paper else "false"
    _write_env(env_updates)

    # Reload env vars into os.environ so new engine picks them up
    for k, v in _read_env().items():
        os.environ[k] = v

    engine = _get_engine(request)
    if skip_next:
        engine.skip_to_next_market()

    asyncio.create_task(_restart_engine(request.app, paper, underlying))
    return web.json_response({"ok": True, "message": "Restarting bot with new settings\u2026"})


# ─── Server setup ─────────────────────────────────────────────────────────────

def _make_app(engines: dict) -> web.Application:
    app = web.Application()
    app["engines"] = engines
    app.router.add_get("/",              handle_index)
    app.router.add_get("/api/state",     handle_state)
    app.router.add_get("/api/settings",  handle_settings_get)
    app.router.add_post("/api/settings", handle_settings_post)
    app.router.add_post("/api/pause",    handle_pause)
    app.router.add_post("/api/resume",   handle_resume)
    app.router.add_post("/api/reset",    handle_reset)
    return app


async def run_web_bot(underlyings=None, paper: bool = False, host: str = "0.0.0.0", port: int = 8090) -> None:
    """Start one bot engine per underlying and the aiohttp web server concurrently."""
    from bot.bot_engine import BotEngine

    if underlyings is None:
        underlyings = ["BTC"]

    engines = {u: BotEngine(underlying=u, paper=paper) for u in underlyings}
    app = _make_app(engines)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"\n  Dashboard -> http://{host}:{port}  (tracking: {', '.join(underlyings)})\n")
    log.info("Web server started on http://%s:%d — engines: %s", host, port, ", ".join(underlyings))

    for engine in engines.values():
        asyncio.create_task(engine.run())

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
