"""Aiohttp web dashboard for the Spread Capture Market Making Bot.

Run via:  python spread_main.py --web --paper
Then open http://localhost:8091
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from aiohttp import web

from spreadcapture.engine import SpreadCaptureEngine
from spreadcapture.config import (
    TARGET_SPREAD, ORDER_SIZE, MAX_INVENTORY,
    REFRESH_INTERVAL_MS, EDGE_THRESHOLD, INVENTORY_SKEW,
    MIN_PRICE, MAX_PRICE, STOP_BEFORE_END_MS, MAX_LOSS,
    ORDERBOOK_DEPTH,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Spread Capture Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--surface:#161b22;--border:#30363d;
  --text:#c9d1d9;--muted:#8b949e;
  --green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--purple:#bc8cff;
}
body{background:var(--bg);color:var(--text);font-family:'Cascadia Code',Consolas,monospace;font-size:13px;overflow-y:auto}

/* ── Header ── */
#hdr{position:sticky;top:0;z-index:100;display:flex;align-items:center;padding:7px 14px;background:var(--surface);border-bottom:1px solid var(--border);gap:10px}
#hdr .name{font-weight:bold;color:var(--purple);letter-spacing:1px;font-size:12px}
#hdr .status{flex:1;text-align:center;font-weight:bold}
#hdr .pnl{min-width:130px;text-align:right;font-weight:bold;font-size:14px}
.hdr-btn{background:none;border:1px solid var(--border);color:var(--muted);cursor:pointer;font:inherit;font-size:11px;letter-spacing:.3px;padding:3px 10px;border-radius:3px}
.hdr-btn:hover{border-color:var(--text);color:var(--text)}
.hdr-btn.pause-active{border-color:var(--yellow);color:var(--yellow)}
#h-paper{display:none;background:var(--yellow);color:#0d1117;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;letter-spacing:1px}

/* ── Tabs ── */
#tab-nav{display:flex;gap:2px;padding:0 14px;background:var(--surface);border-bottom:1px solid var(--border)}
.tab-btn{background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);cursor:pointer;font:inherit;font-size:12px;letter-spacing:.5px;padding:8px 16px;text-transform:uppercase}
.tab-btn.active{border-bottom-color:var(--purple);color:var(--purple)}
.tab-btn:hover:not(.active){color:var(--text)}

/* ── Main grid ── */
#ob-section{display:grid;grid-template-columns:210px 1fr 1fr;height:290px;gap:1px;background:var(--border)}
.panel{background:var(--surface);display:flex;flex-direction:column;overflow:hidden}
.ptitle{padding:5px 10px;font-weight:bold;border-bottom:1px solid var(--border);font-size:10px;text-transform:uppercase;letter-spacing:.6px;flex-shrink:0}
#pnl-panel .ptitle{color:var(--purple)}
#ob-up .ptitle{color:var(--green)}
#ob-dn .ptitle{color:var(--red)}
#info{padding:8px 10px;overflow-y:auto;flex:1;font-size:12px}
.isect{margin-bottom:10px}
.isect-title{color:var(--purple);font-size:9px;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;padding-bottom:2px;border-bottom:1px solid var(--border)}
.irow{display:flex;justify-content:space-between;padding:1px 0}
.ikey{color:var(--muted)}.ival{font-weight:bold;text-align:right}

/* ── Quote bar (shows active bids) ── */
.price-bar{display:flex;justify-content:space-between;align-items:center;padding:5px 10px;border-bottom:1px solid var(--border);background:rgba(0,0,0,.2);flex-shrink:0}
.pb-bid{color:var(--green);font-weight:bold;font-size:14px}
.pb-ask{color:var(--red);font-weight:bold;font-size:14px}
.pb-quote{color:var(--purple);font-weight:bold;font-size:14px}
.pb-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.pb-mid{color:var(--text);font-size:11px}
.ob-body{flex:1;overflow-y:auto;padding:2px 0}
.ob-row{display:grid;grid-template-columns:38px 1fr 1fr;padding:2px 10px;gap:4px;line-height:1.5}
.ob-row.best{font-weight:bold}
.ob-row.quote-row{background:rgba(188,140,255,.10);font-weight:bold}
.ob-sep{border-top:1px solid var(--border);margin:3px 10px}
.ob-empty{padding:8px 10px;color:var(--muted)}
.ask{color:var(--red)}.bid{color:var(--green)}.ob-sz{text-align:right;color:var(--muted)}.mark{color:var(--yellow);margin-left:2px}
.our-bid{color:var(--purple)}

/* ── Quote status banner ── */
#quote-banner{background:var(--surface);border-top:1px solid var(--border);padding:8px 16px;display:flex;gap:24px;align-items:center;font-size:12px}
.qb-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.5px;margin-right:6px}
.qb-val{font-weight:bold}

/* ── Stats ── */
#stats-section{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border-top:1px solid var(--border)}
.stat-box{background:var(--surface);padding:14px 16px}
.stat-label{font-size:9px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:6px}
.stat-value{font-size:26px;font-weight:bold;line-height:1.1}
.stat-sub{font-size:11px;color:var(--muted);margin-top:4px}

/* ── Charts ── */
.chart-box{background:var(--surface);padding:10px 14px 12px;border-top:1px solid var(--border)}
.chart-header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.chart-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}
.chart-subtitle{font-size:11px}
.chart-wrap{position:relative;height:130px}

/* ── Fills list ── */
#fills-box{background:var(--surface);border-top:1px solid var(--border)}
.box-header{display:flex;justify-content:space-between;align-items:center;padding:6px 12px;border-bottom:1px solid var(--border)}
.box-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}
.fill-row{display:grid;grid-template-columns:55px 55px 100px 1fr;padding:4px 12px;border-bottom:1px solid rgba(48,54,61,.4);font-size:12px;align-items:center}
.fill-side-up{color:var(--green);font-weight:bold}
.fill-side-dn{color:var(--red);font-weight:bold}
.fill-price{font-weight:bold}
.fill-time{color:var(--muted);font-size:11px}
.fill-profit{text-align:right}
.fill-direction-buy{color:var(--purple);font-size:10px;font-weight:bold}
.fill-direction-sell{color:var(--yellow);font-size:10px;font-weight:bold}

/* ── Log ── */
#log-panel{border-top:1px solid var(--border);background:var(--bg);display:flex;flex-direction:column;height:140px}
#log-panel .ptitle{color:var(--muted);flex-shrink:0}
#log-body{flex:1;overflow-y:auto;padding:4px 10px;font-size:11.5px}
.ll{padding:1px 0;white-space:nowrap;color:var(--muted)}.ll:last-child{color:var(--text)}

/* ── Settings ── */
#tab-settings{display:none;padding:20px;max-width:540px;margin:0 auto}
.s-section{margin-bottom:24px}
.s-title{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.s-row{margin-bottom:12px}
.s-label{display:block;font-size:11px;color:var(--muted);margin-bottom:4px}
.s-input{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);font:inherit;font-size:12px;padding:6px 10px;border-radius:4px;outline:none}
.s-input:focus{border-color:var(--purple)}
.s-grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.s-save{width:100%;background:var(--purple);border:none;color:#0d1117;cursor:pointer;font:inherit;font-size:13px;font-weight:bold;padding:10px;border-radius:4px;margin-top:8px}
.s-save:hover{opacity:.9}
.s-msg{margin-top:10px;font-size:12px;text-align:center;min-height:18px}
.s-msg.ok{color:var(--green)} .s-msg.err{color:var(--red)}
.s-hint{font-size:10px;color:var(--muted);margin-top:3px}

/* utils */
.green{color:var(--green)!important}.red{color:var(--red)!important}
.yellow{color:var(--yellow)!important}.blue{color:var(--blue)!important}.purple{color:var(--purple)!important}
.bold{font-weight:bold}
.blink{animation:blink 1s step-start infinite}
@keyframes blink{50%{opacity:0}}
</style>
</head>
<body>

<!-- Header -->
<div id="hdr">
  <span class="name">◈ SPREAD CAPTURE BOT</span>
  <span id="h-paper">PAPER</span>
  <span class="status" id="h-status">CONNECTING…</span>
  <span class="pnl" id="h-pnl">PnL: $0.00</span>
  <button class="hdr-btn" id="btn-pause" onclick="togglePause()">Pause</button>
  <button class="hdr-btn" onclick="doReset()">Reset</button>
</div>

<!-- Tabs -->
<div id="tab-nav">
  <button class="tab-btn active" onclick="switchTab('dashboard')">Dashboard</button>
  <button class="tab-btn" onclick="switchTab('settings')">⚙ Settings</button>
</div>

<!-- Dashboard -->
<div id="tab-dashboard">

<!-- Orderbook section -->
<div id="ob-section">
  <div class="panel" id="pnl-panel">
    <div class="ptitle">Market &amp; Position</div>
    <div id="info">—</div>
  </div>
  <div class="panel" id="ob-up">
    <div class="ptitle">UP orderbook</div>
    <div class="price-bar">
      <div><div class="pb-label">Bid</div><div class="pb-bid" id="pb-up-bid">—</div></div>
      <div style="text-align:center"><div class="pb-label">Quote</div><div class="pb-quote" id="pb-up-quote">—</div></div>
      <div style="text-align:right"><div class="pb-label">Ask</div><div class="pb-ask" id="pb-up-ask">—</div></div>
    </div>
    <div class="ob-body" id="ob-up-body"></div>
  </div>
  <div class="panel" id="ob-dn">
    <div class="ptitle">DOWN orderbook</div>
    <div class="price-bar">
      <div><div class="pb-label">Bid</div><div class="pb-bid" id="pb-dn-bid">—</div></div>
      <div style="text-align:center"><div class="pb-label">Quote</div><div class="pb-quote" id="pb-dn-quote">—</div></div>
      <div style="text-align:right"><div class="pb-label">Ask</div><div class="pb-ask" id="pb-dn-ask">—</div></div>
    </div>
    <div class="ob-body" id="ob-dn-body"></div>
  </div>
</div>

<!-- Active quote banner -->
<div id="quote-banner">
  <span><span class="qb-label">BID</span><span class="qb-val" id="qb-bid">—</span></span>
  <span><span class="qb-label">ASK</span><span class="qb-val" id="qb-ask">—</span></span>
  <span><span class="qb-label">Spread</span><span class="qb-val" id="qb-spread">—</span></span>
  <span><span class="qb-label">Edge</span><span class="qb-val" id="qb-edge">—</span></span>
  <span><span class="qb-label">Next refresh</span><span class="qb-val" id="qb-refresh">—</span></span>
  <span style="flex:1"></span>
  <span><span class="qb-label">Inventory</span><span class="qb-val" id="qb-inventory">UP:0 DN:0</span></span>
  <span><span class="qb-label">Sold</span><span class="qb-val" id="qb-sold">UP:0 DN:0</span></span>
</div>

<!-- Stats -->
<div id="stats-section">
  <div class="stat-box">
    <div class="stat-label">Unrealised PnL</div>
    <div class="stat-value" id="sv-unrealised">$0.00</div>
    <div class="stat-sub" id="ss-unrealised">0 matched pairs</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">Total Fills</div>
    <div class="stat-value" id="sv-fills">0</div>
    <div class="stat-sub" id="ss-fills">UP: 0  DN: 0</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">↘ If DOWN wins</div>
    <div class="stat-value" id="sv-if-dn">—</div>
  </div>
  <div class="stat-box">
    <div class="stat-label">↗ If UP wins</div>
    <div class="stat-value" id="sv-if-up">—</div>
  </div>
</div>

<!-- Charts -->
<div class="chart-box">
  <div class="chart-header">
    <span class="chart-title">Mid Prices &amp; Quotes</span>
    <span class="chart-subtitle" id="price-sub"></span>
  </div>
  <div class="chart-wrap"><canvas id="chart-prices"></canvas></div>
</div>

<div class="chart-box">
  <div class="chart-header">
    <span class="chart-title">Captured Spread</span>
    <span class="chart-subtitle" id="spread-sub"></span>
  </div>
  <div class="chart-wrap"><canvas id="chart-spread"></canvas></div>
</div>

<div class="chart-box">
  <div class="chart-header">
    <span class="chart-title">Inventory (shares)</span>
    <span class="chart-subtitle" id="inv-sub"></span>
  </div>
  <div class="chart-wrap"><canvas id="chart-inventory"></canvas></div>
</div>

<!-- Fills -->
<div id="fills-box">
  <div class="box-header">
    <span class="box-title">Recent Fills</span>
    <span id="fills-count" style="color:var(--muted);font-size:11px">0 total</span>
  </div>
  <div id="fills-body"><div style="padding:8px 12px;color:var(--muted)">No fills yet</div></div>
</div>

<!-- Log -->
<div id="log-panel">
  <div class="ptitle">Log</div>
  <div id="log-body"></div>
</div>

</div><!-- end tab-dashboard -->

<!-- Settings -->
<div id="tab-settings">
  <div class="s-section">
    <div class="s-title">Strategy Parameters</div>
    <div class="s-grid2">
      <div class="s-row">
        <label class="s-label">Target Spread</label>
        <input class="s-input" type="number" id="s-spread" step="0.01" min="0.01" max="0.5">
        <div class="s-hint">1.0 − bid_up − bid_down ≥ this value</div>
      </div>
      <div class="s-row">
        <label class="s-label">Order Size (shares/side)</label>
        <input class="s-input" type="number" id="s-size" step="1" min="1" max="100">
      </div>
      <div class="s-row">
        <label class="s-label">Max Inventory per side</label>
        <input class="s-input" type="number" id="s-maxinv" step="1" min="1">
      </div>
      <div class="s-row">
        <label class="s-label">Refresh Interval (ms)</label>
        <input class="s-input" type="number" id="s-refresh" step="100" min="500">
      </div>
      <div class="s-row">
        <label class="s-label">Edge Threshold</label>
        <input class="s-input" type="number" id="s-edge" step="0.005" min="0.001">
        <div class="s-hint">Min edge above break-even</div>
      </div>
      <div class="s-row">
        <label class="s-label">Inventory Skew (0–1)</label>
        <input class="s-input" type="number" id="s-skew" step="0.1" min="0" max="1">
      </div>
      <div class="s-row">
        <label class="s-label">Min Price</label>
        <input class="s-input" type="number" id="s-minprice" step="0.01" min="0" max="0.99">
      </div>
      <div class="s-row">
        <label class="s-label">Max Price</label>
        <input class="s-input" type="number" id="s-maxprice" step="0.01" min="0.01" max="1">
      </div>
      <div class="s-row">
        <label class="s-label">Stop before end (ms)</label>
        <input class="s-input" type="number" id="s-stop" step="1000" min="0">
      </div>
      <div class="s-row">
        <label class="s-label">Max Loss ($)</label>
        <input class="s-input" type="number" id="s-maxloss" step="1" min="1">
      </div>
    </div>
  </div>
  <div class="s-section">
    <div class="s-title">Sell-Side Parameters</div>
    <div class="s-grid2">
      <div class="s-row">
        <label class="s-label">Sell-Side Enabled</label>
        <select class="s-input" id="s-sellside">
          <option value="true">Enabled</option>
          <option value="false">Disabled</option>
        </select>
      </div>
      <div class="s-row">
        <label class="s-label">Min Inventory to Sell</label>
        <input class="s-input" type="number" id="s-mininvsell" step="1" min="1">
        <div class="s-hint">Min shares held before posting asks</div>
      </div>
      <div class="s-row">
        <label class="s-label">Ask Offset Multiplier</label>
        <input class="s-input" type="number" id="s-askoffset" step="0.1" min="0.1" max="3">
        <div class="s-hint">1.0 = symmetric around mid</div>
      </div>
    </div>
  </div>
  <button class="s-save" onclick="saveSettings()">Save &amp; Apply</button>
  <div class="s-msg" id="s-msg"></div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const pnlStr = v => (v >= 0 ? '+' : '') + '$' + v.toFixed(2);
const pnlCls = v => v >= 0 ? 'green' : 'red';
const timeCls = s => s < 30 ? 'red blink' : s < 60 ? 'yellow' : 'green';
const timeFmt = s => String(Math.floor(s/60)).padStart(2,'0') + ':' + String(Math.floor(s%60)).padStart(2,'0');
const irow = (k,v) => `<div class="irow"><span class="ikey">${k}</span><span class="ival">${v}</span></div>`;

// ── Charts ──────────────────────────────────────────────────────────────
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';

const BASE_OPTS = {
  animation:false, responsive:true, maintainAspectRatio:false,
  plugins:{legend:{display:true,position:'bottom',labels:{boxWidth:10,font:{size:10},padding:8,color:'#8b949e'}},tooltip:{enabled:false}},
  scales:{x:{display:false},y:{grid:{color:'rgba(48,54,61,.6)'},ticks:{font:{size:10},maxTicksLimit:5,color:'#8b949e'}}}
};
function mkChart(id,datasets,yExtra={}) {
  return new Chart($(id),{type:'line',data:{labels:[],datasets},options:{...BASE_OPTS,scales:{x:{display:false},y:{...BASE_OPTS.scales.y,...yExtra}}}});
}

const priceChart = mkChart('chart-prices',[
  {label:'UP mid',   borderColor:'#3fb950', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'UP quote', borderColor:'#bc8cff', data:[], pointRadius:0, borderWidth:1.5, tension:.2, borderDash:[4,3]},
  {label:'DN mid',   borderColor:'#f85149', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'DN quote', borderColor:'#d29922', data:[], pointRadius:0, borderWidth:1.5, tension:.2, borderDash:[4,3]},
],{min:0,max:1,ticks:{callback:v=>v.toFixed(2)}});

const spreadChart = mkChart('chart-spread',[
  {label:'Spread',       borderColor:'#bc8cff', data:[], pointRadius:0, borderWidth:2, tension:.2},
  {label:'Target',       borderColor:'rgba(188,140,255,.3)', data:[], pointRadius:0, borderWidth:1, borderDash:[6,3]},
],{ticks:{callback:v=>v.toFixed(3)}});

const invChart = mkChart('chart-inventory',[
  {label:'UP shares', borderColor:'#3fb950', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
  {label:'DN shares', borderColor:'#f85149', data:[], pointRadius:0, borderWidth:1.5, tension:.2},
],{ticks:{callback:v=>Math.round(v)}});

const MAX_PTS = 120;
let lastChartT = 0, currentSlug = null, _targetSpread = 0.03;

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
  [priceChart, spreadChart, invChart].forEach(c => {
    c.data.labels = []; c.data.datasets.forEach(d => d.data = []); c.update('none');
  });
}

function updateCharts(s) {
  const now = Date.now();
  if (now - lastChartT < 1000) return;
  lastChartT = now;
  const slug = s.market?.slug ?? null;
  if (slug !== currentSlug) { currentSlug = slug; resetCharts(); }
  const t = new Date().toLocaleTimeString('no',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});

  const upBid = s.orderbook_up?.bids[0]?.price ?? null;
  const upAsk = s.orderbook_up?.asks[0]?.price ?? null;
  const dnBid = s.orderbook_down?.bids[0]?.price ?? null;
  const dnAsk = s.orderbook_down?.asks[0]?.price ?? null;
  const midUp = (upBid!=null&&upAsk!=null) ? (upBid+upAsk)/2 : null;
  const midDn = (dnBid!=null&&dnAsk!=null) ? (dnBid+dnAsk)/2 : null;
  const qUp = s.quote?.quote_up ?? null;
  const qDn = s.quote?.quote_down ?? null;
  const spread = (qUp != null && qDn != null) ? (1 - qUp - qDn) : null;

  chartPush(priceChart, t, midUp, qUp, midDn, qDn);
  if (spread != null) chartPush(spreadChart, t, spread, _targetSpread);
  chartPush(invChart, t, s.positions.up_shares, s.positions.down_shares);
}

// ── Orderbook renderer ──────────────────────────────────────────────────
function renderOB(bodyId, ob, quotePx) {
  const el = $(bodyId);
  const side = bodyId.includes('up') ? 'up' : 'dn';
  if (!ob || (!ob.asks.length && !ob.bids.length)) {
    el.innerHTML = '<div class="ob-empty">Loading…</div>';
    [`pb-${side}-bid`,`pb-${side}-ask`].forEach(id => { const e=$(id); if(e) e.textContent='—'; });
    return;
  }
  const bestBid = ob.bids[0]?.price ?? null;
  const bestAsk = ob.asks[0]?.price ?? null;
  $(`pb-${side}-bid`).textContent = bestBid != null ? bestBid.toFixed(4) : '—';
  $(`pb-${side}-ask`).textContent = bestAsk != null ? bestAsk.toFixed(4) : '—';

  let h = '';
  const asks = [...ob.asks].slice(0,8).reverse();
  asks.forEach((lvl,i,arr) => {
    const best = i === arr.length - 1;
    h += `<div class="ob-row${best?' best':''}"><span class="ask">ASK</span><span>${lvl.price.toFixed(4)}${best?'<span class="mark">◄</span>':''}</span><span class="ob-sz">${lvl.size.toFixed(1)}</span></div>`;
  });
  h += '<div class="ob-sep"></div>';
  let quoteInserted = false;
  ob.bids.slice(0,8).forEach((lvl,i) => {
    // Insert our quote row if quote price is between this bid and previous bid
    if (!quoteInserted && quotePx != null && lvl.price < quotePx) {
      h += `<div class="ob-row quote-row"><span class="our-bid">OUR</span><span class="our-bid">${quotePx.toFixed(4)}</span><span class="ob-sz our-bid">→</span></div>`;
      quoteInserted = true;
    }
    h += `<div class="ob-row${i===0?' best':''}"><span class="bid">BID</span><span>${lvl.price.toFixed(4)}${i===0?'<span class="mark">◄</span>':''}</span><span class="ob-sz">${lvl.size.toFixed(1)}</span></div>`;
  });
  if (!quoteInserted && quotePx != null) {
    h += `<div class="ob-row quote-row"><span class="our-bid">OUR</span><span class="our-bid">${quotePx.toFixed(4)}</span><span class="ob-sz our-bid">→</span></div>`;
  }
  el.innerHTML = h;
}

// ── Main state renderer ─────────────────────────────────────────────────
let _paused = false;

function applyState(s) {
  _targetSpread = s.config?.target_spread ?? 0.03;

  // Header
  $('h-paper').style.display = s.paper ? 'inline' : 'none';
  let stHtml = esc(s.status);
  if (s.is_halted)           stHtml = `<span class="red bold">⚠ HALTED</span>`;
  else if (s.status==='TRADING')  stHtml = `<span class="green bold">TRADING</span>`;
  else if (s.status==='SCANNING') stHtml = `<span class="yellow">SCANNING…</span>`;
  $('h-status').innerHTML = 'Status: ' + stHtml;
  const tot = s.pnl.total;
  $('h-pnl').innerHTML = `<span class="${pnlCls(tot)} bold">PnL: ${pnlStr(tot)}</span>`;
  if (s.is_paused !== _paused) {
    _paused = s.is_paused;
    const btn = $('btn-pause');
    if (btn) { btn.textContent = _paused ? 'Resume' : 'Pause'; btn.classList.toggle('pause-active', _paused); }
  }

  // Info panel
  const q = s.quote;
  let info = '<div class="isect"><div class="isect-title">Market</div>';
  info += irow('Underlying', `<span class="purple bold">${esc(s.underlying)}</span>`);
  if (s.market) {
    const slug = s.market.slug.length > 22 ? s.market.slug.slice(0,22)+'…' : s.market.slug;
    info += irow('Slug', `<span title="${esc(s.market.slug)}">${esc(slug)}</span>`);
    info += irow('Time left', `<span class="${timeCls(s.market.seconds_left)} bold">${timeFmt(s.market.seconds_left)}</span>`);
  } else {
    info += irow('Pair', '<span class="yellow">Scanning…</span>');
  }
  info += '</div>';
  info += '<div class="isect"><div class="isect-title">PnL</div>';
  info += irow('Unrealised',  `<span class="${pnlCls(s.pnl.unrealised)} bold">${pnlStr(s.pnl.unrealised)}</span>`);
  info += irow('Realised',    `<span class="${pnlCls(s.pnl.realised)} bold">${pnlStr(s.pnl.realised)}</span>`);
  info += irow('Sell PnL',    `<span class="${pnlCls(s.pnl.sell_realized||0)} bold">${pnlStr(s.pnl.sell_realized||0)}</span>`);
  info += irow('Session',     `<span class="${pnlCls(s.pnl.total)} bold">${pnlStr(s.pnl.total)}</span>`);
  info += irow('Fills',       `<span class="bold">${s.total_fills}</span>`);
  info += '</div>';
  info += '<div class="isect"><div class="isect-title">Position</div>';
  info += irow('UP shares',  `<span class="green bold">${s.positions.up_shares}</span>`);
  info += irow('DN shares',  `<span class="red bold">${s.positions.down_shares}</span>`);
  if (s.positions.up_shares_sold)   info += irow('UP sold', `<span class="green">${s.positions.up_shares_sold}</span>`);
  if (s.positions.down_shares_sold) info += irow('DN sold', `<span class="red">${s.positions.down_shares_sold}</span>`);
  if (s.positions.avg_up_price) info += irow('Avg UP', `<span class="green">${s.positions.avg_up_price.toFixed(4)}</span>`);
  if (s.positions.avg_down_price) info += irow('Avg DN', `<span class="red">${s.positions.avg_down_price.toFixed(4)}</span>`);
  info += '</div>';
  $('info').innerHTML = info;

  // Orderbooks with our quote overlaid
  renderOB('ob-up-body', s.orderbook_up, q?.quote_up ?? null);
  renderOB('ob-dn-body', s.orderbook_down, q?.quote_down ?? null);

  // Quote price bar
  $('pb-up-quote').textContent = q ? q.quote_up.toFixed(4) : '—';
  $('pb-dn-quote').textContent = q ? q.quote_down.toFixed(4) : '—';

  // Quote banner
  if (q) {
    $('qb-bid').innerHTML = `<span class="purple">UP@${q.quote_up.toFixed(3)}\xd7${q.size_up}</span> / <span class="yellow">DN@${q.quote_down.toFixed(3)}\xd7${q.size_down}</span>`;
    if (q.has_asks) {
      $('qb-ask').innerHTML = `<span class="green">UP@${q.ask_up.toFixed(3)}\xd7${q.size_ask_up}</span> / <span class="red">DN@${q.ask_down.toFixed(3)}\xd7${q.size_ask_down}</span>`;
    } else {
      $('qb-ask').innerHTML = `<span class="muted">\u2014</span>`;
    }
    const spd = (1 - q.quote_up - q.quote_down);
    $('qb-spread').innerHTML = `<span class="${spd >= _targetSpread ? 'green' : 'red'}">${spd.toFixed(4)}</span>`;
    $('qb-edge').innerHTML = `<span class="green">${(spd - _targetSpread).toFixed(4)}</span>`;
  } else {
    $('qb-bid').innerHTML = `<span class="muted">\u2014</span>`;
    $('qb-ask').innerHTML = `<span class="muted">\u2014</span>`;
    $('qb-spread').textContent = '\u2014';
    $('qb-edge').textContent = '\u2014';
  }
  $('qb-inventory').innerHTML = `<span class="green">UP:${s.positions.up_shares}</span> <span class="red">DN:${s.positions.down_shares}</span>`;
  $('qb-sold').innerHTML = `<span class="green">UP:${s.positions.up_shares_sold||0}</span> <span class="red">DN:${s.positions.down_shares_sold||0}</span>`;
  $('qb-refresh').textContent = s.next_refresh_ms != null ? `${Math.round(s.next_refresh_ms)}ms` : '—';

  // Stats
  const pnl = s.pnl;
  $('sv-unrealised').innerHTML = `<span class="${pnlCls(pnl.unrealised)}">${pnlStr(pnl.unrealised)}</span>`;
  const pos = s.positions;
  const matched = Math.min(pos.up_shares, pos.down_shares);
  $('ss-unrealised').textContent = `${matched} matched pair${matched===1?'':'s'}`;
  $('sv-fills').textContent = s.total_fills;
  $('ss-fills').innerHTML = `<span class="green">UP: ${pos.up_shares}</span>  <span class="red">DN: ${pos.down_shares}</span>`;

  // Scenario PnL
  const upWins = pos.up_shares * 1.0 - (pos.up_shares * (pos.avg_up_price||0)) - (pos.down_shares * (pos.avg_down_price||0));
  const dnWins = pos.down_shares * 1.0 - (pos.up_shares * (pos.avg_up_price||0)) - (pos.down_shares * (pos.avg_down_price||0));
  if (pos.up_shares > 0 || pos.down_shares > 0) {
    $('sv-if-up').innerHTML = `<span class="${pnlCls(upWins)}">${pnlStr(upWins)}</span>`;
    $('sv-if-dn').innerHTML = `<span class="${pnlCls(dnWins)}">${pnlStr(dnWins)}</span>`;
  }

  // Fills list
  $('fills-count').textContent = `${s.total_fills} total`;
  const fills = s.recent_fills || [];
  if (fills.length) {
    $('fills-body').innerHTML = fills.map(f => {
      const sideCls = f.side === 'UP' ? 'fill-side-up' : 'fill-side-dn';
      const dirCls  = f.direction === 'SELL' ? 'fill-direction-sell' : 'fill-direction-buy';
      const dirLabel = f.direction === 'SELL' ? 'SELL' : 'BUY';
      return `<div class="fill-row">
        <span class="${sideCls}">${f.side}</span>
        <span class="${dirCls}">${dirLabel}</span>
        <span class="fill-price">${f.shares}sh @ ${f.price.toFixed(4)}</span>
        <span class="fill-time">${esc(f.time)}</span>
      </div>`;
    }).join('');
  } else {
    $('fills-body').innerHTML = '<div style="padding:8px 12px;color:var(--muted)">No fills yet</div>';
  }

  updateCharts(s);

  // Log
  const logEl = $('log-body');
  const atBot = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 5;
  logEl.innerHTML = s.logs.map(l => `<div class="ll">${esc(l)}</div>`).join('');
  if (atBot) logEl.scrollTop = logEl.scrollHeight;
}

// ── Settings form ────────────────────────────────────────────────────────
async function loadSettings() {
  try {
    const r = await fetch('/api/spread/settings');
    if (!r.ok) return;
    const s = await r.json();
    $('s-spread').value    = s.target_spread   ?? 0.03;
    $('s-size').value      = s.order_size       ?? 10;
    $('s-maxinv').value    = s.max_inventory    ?? 50;
    $('s-refresh').value   = s.refresh_interval_ms ?? 2000;
    $('s-edge').value      = s.edge_threshold   ?? 0.01;
    $('s-skew').value      = s.inventory_skew   ?? 0.5;
    $('s-minprice').value  = s.min_price        ?? 0;
    $('s-maxprice').value  = s.max_price        ?? 1;
    $('s-stop').value      = s.stop_before_end_ms ?? 30000;
    $('s-maxloss').value   = s.max_loss         ?? 50;
    $('s-sellside').value   = String(s.sell_side_enabled ?? true);
    $('s-mininvsell').value = s.min_inventory_to_sell ?? 1;
    $('s-askoffset').value  = s.ask_offset_multiplier ?? 1.0;
  } catch {}
}

async function saveSettings() {
  const msg = $('s-msg');
  msg.textContent = 'Saving…'; msg.className = 's-msg';
  try {
    const body = {
      target_spread:       parseFloat($('s-spread').value),
      order_size:          parseInt($('s-size').value),
      max_inventory:       parseInt($('s-maxinv').value),
      refresh_interval_ms: parseInt($('s-refresh').value),
      edge_threshold:      parseFloat($('s-edge').value),
      inventory_skew:      parseFloat($('s-skew').value),
      min_price:           parseFloat($('s-minprice').value),
      max_price:           parseFloat($('s-maxprice').value),
      stop_before_end_ms:  parseInt($('s-stop').value),
      max_loss:             parseFloat($('s-maxloss').value),
      sell_side_enabled:    $('s-sellside').value === 'true',
      min_inventory_to_sell: parseInt($('s-mininvsell').value),
      ask_offset_multiplier: parseFloat($('s-askoffset').value),
    };
    const r = await fetch('/api/spread/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    if (d.ok) { msg.textContent = 'Saved!'; msg.className = 's-msg ok'; }
    else { msg.textContent = d.error || 'Error'; msg.className = 's-msg err'; }
  } catch(e) { msg.textContent = 'Network error'; msg.className = 's-msg err'; }
}

// ── Tab switching ────────────────────────────────────────────────────────
function switchTab(name) {
  $('tab-dashboard').style.display = name === 'dashboard' ? '' : 'none';
  $('tab-settings').style.display  = name === 'settings'  ? 'block' : 'none';
  document.querySelectorAll('.tab-btn').forEach((b,i) => {
    b.classList.toggle('active', (i===0 && name==='dashboard') || (i===1 && name==='settings'));
  });
  if (name === 'settings') loadSettings();
}

// ── Pause / Reset ─────────────────────────────────────────────────────────
async function togglePause() {
  const ep = _paused ? '/api/spread/resume' : '/api/spread/pause';
  await fetch(ep, {method:'POST'});
  _paused = !_paused;
  const btn = $('btn-pause');
  btn.textContent = _paused ? 'Resume' : 'Pause';
  btn.classList.toggle('pause-active', _paused);
}
async function doReset() {
  if (!confirm('Reset all statistics? This cannot be undone.')) return;
  await fetch('/api/spread/reset', {method:'POST'});
}

// ── Polling loop ─────────────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/spread/state');
    if (r.ok) applyState(await r.json());
  } catch {}
  setTimeout(poll, 500);
}

poll();
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────

def _build_state(engine: SpreadCaptureEngine, next_refresh_ms: float) -> dict:
    """Serialise engine state to a JSON-safe dict for the frontend."""
    pair = engine._current_pair
    ob_up = engine.ob_up
    ob_down = engine.ob_down
    pos = engine.tracker.position

    def _ob_to_dict(ob):
        if ob is None:
            return {"bids": [], "asks": []}
        return {
            "bids": [{"price": l.price, "size": l.size} for l in ob.top_bids(ORDERBOOK_DEPTH)],
            "asks": [{"price": l.price, "size": l.size} for l in ob.top_asks(ORDERBOOK_DEPTH)],
        }

    market_info = None
    if pair is not None:
        market_info = {
            "slug": pair.short_label(),
            "seconds_left": max(0, pair.ms_until_end / 1000),
        }

    q = engine.current_quotes
    quote_dict = None
    if q is not None:
        quote_dict = {
            "quote_up":      q.quote_up,
            "quote_down":    q.quote_down,
            "size_up":       q.size_up,
            "size_down":     q.size_down,
            "spread":        q.spread,
            "ask_up":        q.ask_up,
            "ask_down":      q.ask_down,
            "size_ask_up":   q.size_ask_up,
            "size_ask_down": q.size_ask_down,
            "has_asks":      q.has_asks,
        }

    positions = {
        "up_shares":        pos.up_shares,
        "down_shares":      pos.down_shares,
        "up_shares_sold":   pos.up_shares_sold,
        "down_shares_sold": pos.down_shares_sold,
        "avg_up_price":     pos.avg_up_price,
        "avg_down_price":   pos.avg_down_price,
        "avg_sum":          pos.avg_sum,
        "share_delta_pct":  pos.share_delta_pct,
    }

    from spreadcapture.config import (
        TARGET_SPREAD, ORDER_SIZE, MAX_INVENTORY, REFRESH_INTERVAL_MS,
        EDGE_THRESHOLD, INVENTORY_SKEW, MIN_PRICE, MAX_PRICE,
        STOP_BEFORE_END_MS, MAX_LOSS,
        SELL_SIDE_ENABLED, MIN_INVENTORY_TO_SELL, ASK_OFFSET_MULTIPLIER,
    )

    return {
        "status":       engine.status,
        "underlying":   engine.underlying,
        "paper":        engine.paper,
        "is_halted":    engine.risk.is_halted,
        "is_paused":    engine.risk.is_halted and "Paused" in engine.risk.halt_reason,
        "halt_reason":  engine.risk.halt_reason,
        "pnl": {
            "unrealised":    engine.tracker.unrealised_pnl,
            "realised":      engine.tracker.realised_pnl,
            "sell_realized": engine.tracker.sell_realized_pnl,
            "total":         engine.tracker.total_pnl,
            "trade_count":   engine.tracker.trade_count,
        },
        "positions":    positions,
        "market":       market_info,
        "quote":        quote_dict,
        "orderbook_up":   _ob_to_dict(ob_up),
        "orderbook_down": _ob_to_dict(ob_down),
        "total_fills":  engine.total_fills,
        "recent_fills": list(engine.recent_fills),
        "logs":         list(engine.log_lines),
        "next_refresh_ms": next_refresh_ms,
        "config": {
            "target_spread":         TARGET_SPREAD,
            "order_size":            ORDER_SIZE,
            "max_inventory":         MAX_INVENTORY,
            "refresh_interval_ms":   REFRESH_INTERVAL_MS,
            "edge_threshold":        EDGE_THRESHOLD,
            "inventory_skew":        INVENTORY_SKEW,
            "min_price":             MIN_PRICE,
            "max_price":             MAX_PRICE,
            "stop_before_end_ms":    STOP_BEFORE_END_MS,
            "max_loss":              MAX_LOSS,
            "sell_side_enabled":     SELL_SIDE_ENABLED,
            "min_inventory_to_sell": MIN_INVENTORY_TO_SELL,
            "ask_offset_multiplier": ASK_OFFSET_MULTIPLIER,
        },
    }


def make_spread_app(engine: SpreadCaptureEngine) -> web.Application:
    """Create and return the aiohttp application."""
    import time as _time

    app = web.Application()
    _last_quote_ms_ref: list = [0.0]

    # ── HTML ──────────────────────────────────────────────────────────────
    async def handle_index(request):
        return web.Response(text=_HTML, content_type="text/html")

    # ── State API ─────────────────────────────────────────────────────────
    async def handle_state(request):
        now = _time.monotonic() * 1000
        from spreadcapture.config import REFRESH_INTERVAL_MS as _RI
        elapsed = now - engine._last_quote_ms
        next_ms = max(0.0, _RI - elapsed)
        return web.Response(
            text=json.dumps(_build_state(engine, next_ms)),
            content_type="application/json",
        )

    # ── Settings ──────────────────────────────────────────────────────────
    async def handle_settings_get(request):
        from spreadcapture import config as sc
        return web.Response(
            text=json.dumps({
                "target_spread":       sc.TARGET_SPREAD,
                "order_size":          sc.ORDER_SIZE,
                "max_inventory":       sc.MAX_INVENTORY,
                "refresh_interval_ms": sc.REFRESH_INTERVAL_MS,
                "edge_threshold":      sc.EDGE_THRESHOLD,
                "inventory_skew":      sc.INVENTORY_SKEW,
                "min_price":           sc.MIN_PRICE,
                "max_price":           sc.MAX_PRICE,
                "stop_before_end_ms":    sc.STOP_BEFORE_END_MS,
                "max_loss":              sc.MAX_LOSS,
                "sell_side_enabled":     sc.SELL_SIDE_ENABLED,
                "min_inventory_to_sell": sc.MIN_INVENTORY_TO_SELL,
                "ask_offset_multiplier": sc.ASK_OFFSET_MULTIPLIER,
            }),
            content_type="application/json",
        )

    async def handle_settings_post(request):
        try:
            body = await request.json()
        except Exception:
            return web.Response(
                text=json.dumps({"ok": False, "error": "Bad JSON"}),
                content_type="application/json",
            )

        from spreadcapture import config as sc
        import spreadcapture.config as sc_mod

        allowed = {
            "target_spread": float, "order_size": int, "max_inventory": int,
            "refresh_interval_ms": int, "edge_threshold": float,
            "inventory_skew": float, "min_price": float, "max_price": float,
            "stop_before_end_ms": int, "max_loss": float,
            "sell_side_enabled": bool, "min_inventory_to_sell": int,
            "ask_offset_multiplier": float,
        }

        updated = []
        for key, cast in allowed.items():
            if key in body:
                try:
                    val = cast(body[key])
                    setattr(sc_mod, key.upper(), val)
                    updated.append(key)
                except (ValueError, TypeError):
                    pass

        # Rebuild risk manager's max_loss limit in-place
        if "max_loss" in body:
            engine.risk._max_loss = float(body["max_loss"])

        engine.log(f"Settings updated: {', '.join(updated)}")
        return web.Response(
            text=json.dumps({"ok": True, "updated": updated}),
            content_type="application/json",
        )

    # ── Control endpoints ─────────────────────────────────────────────────
    async def handle_pause(request):
        engine.pause()
        return web.Response(text='{"ok":true}', content_type="application/json")

    async def handle_resume(request):
        engine.resume()
        return web.Response(text='{"ok":true}', content_type="application/json")

    async def handle_reset(request):
        engine.reset_stats()
        return web.Response(text='{"ok":true}', content_type="application/json")

    # ── Routes ────────────────────────────────────────────────────────────
    app.router.add_get("/",                     handle_index)
    app.router.add_get("/api/spread/state",     handle_state)
    app.router.add_get("/api/spread/settings",  handle_settings_get)
    app.router.add_post("/api/spread/settings", handle_settings_post)
    app.router.add_post("/api/spread/pause",    handle_pause)
    app.router.add_post("/api/spread/resume",   handle_resume)
    app.router.add_post("/api/spread/reset",    handle_reset)

    return app


async def run_spread_web(
    underlying: str = "BTC",
    paper: bool = True,
    port: int = 8091,
) -> None:
    """Launch the spread capture bot + web UI."""
    engine = SpreadCaptureEngine(underlying=underlying, paper=paper)
    app = make_spread_app(engine)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    log.info("Spread Capture UI running at http://localhost:%d", port)
    print(f"\n  ✓ Spread Capture Bot  →  http://localhost:{port}\n")

    await engine.run()

    await runner.cleanup()
