//! RustExecutor — the PyO3-exposed executor class.
//!
//! This is the drop-in replacement for Python's LiveExecutor.
//! Python calls: executor.simulate_buy(side, price, qty, ...)
//! Rust handles: CLOB communication, settlement polling, order management.

use crate::clob::ClobClient;
use crate::types::{FillResult, OrderResponse, Side};
use dashmap::DashMap;
use pyo3::prelude::*;
use std::sync::Arc;
use std::time::Instant;
use tokio::runtime::Runtime;

/// Safety caps (mirrors Python constants)
const MAX_SINGLE_ORDER_USD: f64 = 8.0;
const MAX_OPEN_EXPOSURE_USD: f64 = 20.0;
const SETTLEMENT_POLL_INTERVAL_MS: u64 = 200;
const SETTLEMENT_MAX_PRIMES: u32 = 15;
const FORCE_SELL_TIMEOUT_S: f64 = 8.0;

#[pyclass]
pub struct RustExecutor {
    live: bool,
    client: Option<Arc<ClobClient>>,
    runtime: Arc<Runtime>,

    // Token IDs for current market
    up_token_id: Option<String>,
    down_token_id: Option<String>,

    // Position tracking
    open_exposure: f64,
    token_position_qty: DashMap<String, f64>,
    token_position_cost: DashMap<String, f64>,
    settlement_confirmed: DashMap<String, bool>,

    // In-flight guard
    buy_in_flight: DashMap<String, bool>,
}

#[pymethods]
impl RustExecutor {
    #[new]
    #[pyo3(signature = (api_key="".to_string(), api_secret="".to_string(), api_passphrase="".to_string(), wallet_address="".to_string(), private_key="".to_string(), live=false))]
    fn new(api_key: String, api_secret: String, api_passphrase: String, wallet_address: String, private_key: String, live: bool) -> Self {
        let runtime = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(4)
                .enable_all()
                .build()
                .expect("Failed to create Tokio runtime"),
        );

        let client = if live && !api_key.is_empty() {
            let c = ClobClient::new(api_key, api_secret, api_passphrase, wallet_address, private_key);
            // Verify connection
            let rt = runtime.clone();
            match rt.block_on(c.ping()) {
                Ok(_) => {
                    println!("[RustExecutor] LIVE MODE connected. Server: OK");
                    Some(Arc::new(c))
                }
                Err(e) => {
                    println!("[RustExecutor] CLOB connection failed: {}. Falling back to PAPER.", e);
                    None
                }
            }
        } else {
            println!("[RustExecutor] PAPER MODE — no real orders.");
            None
        };

        Self {
            live: live && client.is_some(),
            client,
            runtime,
            up_token_id: None,
            down_token_id: None,
            open_exposure: 0.0,
            token_position_qty: DashMap::new(),
            token_position_cost: DashMap::new(),
            settlement_confirmed: DashMap::new(),
            buy_in_flight: DashMap::new(),
        }
    }

    /// Set token IDs for the current market
    fn set_token_ids(&mut self, up_token_id: String, down_token_id: String) {
        if self.up_token_id.as_deref() != Some(&up_token_id)
            || self.down_token_id.as_deref() != Some(&down_token_id)
        {
            // New market — reset tracking
            self.token_position_qty.clear();
            self.token_position_cost.clear();
            self.settlement_confirmed.clear();
        }
        self.up_token_id = Some(up_token_id);
        self.down_token_id = Some(down_token_id);
    }

    /// Get token ID for a side
    fn get_token_id(&self, side: &str) -> Option<String> {
        match side.to_uppercase().as_str() {
            "UP" => self.up_token_id.clone(),
            "DOWN" => self.down_token_id.clone(),
            _ => None,
        }
    }

    /// Check if executor is in live mode
    #[getter]
    fn is_live(&self) -> bool {
        self.live
    }

    /// Get current open exposure
    #[getter]
    fn exposure(&self) -> f64 {
        self.open_exposure
    }

    /// Get balance for a token from CLOB
    fn get_balance(&self, token_id: &str) -> f64 {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            self.runtime
                .block_on(async move { client.get_balance(&tid, 1).await })
                .unwrap_or(0.0)
        } else {
            0.0
        }
    }

    /// Update balance allowance — triggers CLOB chain rescan
    fn update_balance(&self, token_id: &str) {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            self.runtime.block_on(async move {
                let _ = client.update_balance_allowance(&tid, 1).await;
            });
        }
    }

    /// Settlement priming — aggressively poll CLOB after a BUY
    fn prime_settlement(&self, token_id: &str) {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            let confirmed = self.settlement_confirmed.clone();
            let pos_qty = self.token_position_qty.clone();

            self.runtime.spawn(async move {
                for _ in 0..SETTLEMENT_MAX_PRIMES {
                    let _ = client.update_balance_allowance(&tid, 1).await;
                    tokio::time::sleep(std::time::Duration::from_millis(
                        SETTLEMENT_POLL_INTERVAL_MS,
                    ))
                    .await;

                    // Check if balance is now visible
                    if let Ok(bal) = client.get_balance(&tid, 1).await {
                        let expected = pos_qty.get(&tid).map(|v| *v).unwrap_or(0.0);
                        if bal >= expected * 0.9 && bal > 0.5 {
                            confirmed.insert(tid.clone(), true);
                            println!(
                                "[RustExecutor] ✅ Settlement confirmed: {}… ({:.4} shares)",
                                &tid[..16.min(tid.len())],
                                bal
                            );
                            return;
                        }
                    }
                }
                println!(
                    "[RustExecutor] ⚠️ Settlement slow: {}…",
                    &tid[..16.min(tid.len())]
                );
            });
        }
    }

    /// Wait for settlement and return CLOB balance
    fn wait_for_settlement(&self, token_id: &str, max_wait_s: f64) -> f64 {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            let confirmed = self.settlement_confirmed.clone();

            self.runtime.block_on(async move {
                let start = Instant::now();
                let max_wait = std::time::Duration::from_secs_f64(max_wait_s);

                loop {
                    // Update and check
                    let _ = client.update_balance_allowance(&tid, 1).await;
                    if let Ok(bal) = client.get_balance(&tid, 1).await {
                        if bal > 0.5 {
                            confirmed.insert(tid.clone(), true);
                            return bal;
                        }
                    }

                    if start.elapsed() > max_wait {
                        return 0.0;
                    }

                    tokio::time::sleep(std::time::Duration::from_millis(
                        SETTLEMENT_POLL_INTERVAL_MS,
                    ))
                    .await;
                }
            })
        } else {
            0.0
        }
    }

    /// Cancel an order by ID
    fn cancel_order(&self, order_id: &str) -> bool {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let oid = order_id.to_string();
            self.runtime
                .block_on(async move { client.cancel_order(&oid).await })
                .is_ok()
        } else {
            false
        }
    }

    /// Get order status
    fn get_order_status(&self, order_id: &str) -> (String, f64, f64) {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let oid = order_id.to_string();
            match self
                .runtime
                .block_on(async move { client.get_order(&oid).await })
            {
                Ok(status) => (
                    status.status.clone().unwrap_or_default(),
                    status.matched_qty(),
                    status.fill_price(0.0),
                ),
                Err(_) => (String::new(), 0.0, 0.0),
            }
        } else {
            (String::new(), 0.0, 0.0)
        }
    }

    /// Release exposure after market resolution
    fn release_exposure(&mut self, usd_amount: f64) {
        self.open_exposure = (self.open_exposure - usd_amount).max(0.0);
    }

    /// Track a BUY fill
    fn record_buy(&mut self, token_id: &str, qty: f64, cost: f64) {
        *self
            .token_position_qty
            .entry(token_id.to_string())
            .or_insert(0.0) += qty;
        *self
            .token_position_cost
            .entry(token_id.to_string())
            .or_insert(0.0) += cost;
        self.open_exposure += cost;
        self.settlement_confirmed
            .insert(token_id.to_string(), false);
    }

    /// Track a SELL fill
    fn record_sell(&mut self, token_id: &str, qty: f64, _cost: f64) {
        let tracked_qty = self
            .token_position_qty
            .get(token_id)
            .map(|v| *v)
            .unwrap_or(0.0);
        let tracked_cost = self
            .token_position_cost
            .get(token_id)
            .map(|v| *v)
            .unwrap_or(0.0);

        if tracked_qty > 0.0 && tracked_cost > 0.0 {
            let sold_qty = qty.min(tracked_qty);
            let avg_cost = tracked_cost / tracked_qty;
            let released = (avg_cost * sold_qty).min(tracked_cost);
            self.token_position_qty
                .insert(token_id.to_string(), (tracked_qty - sold_qty).max(0.0));
            self.token_position_cost
                .insert(token_id.to_string(), (tracked_cost - released).max(0.0));
            self.open_exposure = (self.open_exposure - released).max(0.0);
        }
    }

    /// Check exposure cap
    fn can_buy(&self, usd: f64) -> bool {
        self.open_exposure + usd <= MAX_OPEN_EXPOSURE_USD
    }

    /// Place a maker BUY order (GTC post_only)
    /// Returns FillResult
    fn place_maker_buy(&self, side: &str, token_id: &str, price: f64, size: f64) -> FillResult {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            let t0 = std::time::Instant::now();

            match self.runtime.block_on(async move {
                client
                    .place_order(
                        &tid,
                        price,
                        size,
                        crate::clob::OrderSide::Buy,
                        crate::clob::OrderType::GtcMaker,
                    )
                    .await
            }) {
                Ok(resp) => parse_fak_response(resp, "BUY", side, price, size, t0),
                Err(e) => {
                    println!("[Rust] BUY {} error: {}", side, &e[..e.len().min(200)]);
                    FillResult {
                        filled: false,
                        reason: format!("BUY_ERROR: {}", &e[..e.len().min(100)]),
                        ..FillResult::default()
                    }
                }
            }
        } else {
            FillResult {
                filled: false,
                reason: "PAPER_MODE".into(),
                ..FillResult::default()
            }
        }
    }

    /// Place a FAK SELL order (taker, immediate)
    fn place_fak_sell(&self, side: &str, token_id: &str, price: f64, size: f64) -> FillResult {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            let t0 = std::time::Instant::now();

            match self.runtime.block_on(async move {
                client
                    .place_order(
                        &tid,
                        price,
                        size,
                        crate::clob::OrderSide::Sell,
                        crate::clob::OrderType::Fak,
                    )
                    .await
            }) {
                Ok(resp) => parse_fak_response(resp, "SELL", side, price, size, t0),
                Err(e) => {
                    println!("[Rust] SELL {} error: {}", side, &e[..e.len().min(200)]);
                    FillResult {
                        filled: false,
                        reason: format!("SELL_ERROR: {}", &e[..e.len().min(100)]),
                        ..FillResult::default()
                    }
                }
            }
        } else {
            FillResult {
                filled: false,
                reason: "PAPER_MODE".into(),
                ..FillResult::default()
            }
        }
    }

    /// Place a maker TP SELL order (GTC post_only)
    fn place_maker_tp_sell(&self, side: &str, token_id: &str, price: f64, size: f64) -> FillResult {
        if let Some(ref client) = self.client {
            let client = client.clone();
            let tid = token_id.to_string();
            let t0 = std::time::Instant::now();

            match self.runtime.block_on(async move {
                client
                    .place_order(
                        &tid,
                        price,
                        size,
                        crate::clob::OrderSide::Sell,
                        crate::clob::OrderType::GtcMaker,
                    )
                    .await
            }) {
                Ok(resp) => parse_fak_response(resp, "SELL_TP", side, price, size, t0),
                Err(e) => {
                    println!("[Rust] SELL_TP {} error: {}", side, &e[..e.len().min(200)]);
                    FillResult {
                        filled: false,
                        reason: format!("SELL_TP_ERROR: {}", &e[..e.len().min(100)]),
                        ..FillResult::default()
                    }
                }
            }
        } else {
            FillResult {
                filled: false,
                reason: "PAPER_MODE".into(),
                ..FillResult::default()
            }
        }
    }

    /// Mode string
    #[getter]
    fn mode(&self) -> &str {
        if self.live {
            "LIVE"
        } else {
            "PAPER"
        }
    }
}

/// Parse FAK/GTC order response into FillResult
fn parse_fak_response(
    resp: OrderResponse,
    action: &str,
    side: &str,
    price: f64,
    size: f64,
    t0: std::time::Instant,
) -> FillResult {
    let latency = t0.elapsed().as_millis() as f64;

    let taking = resp
        .taking_amount
        .as_ref()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0);
    let making = resp
        .making_amount
        .as_ref()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0);

    if taking > 0.0 || making > 0.0 {
        // Deterministic parsing:
        // BUY:  takingAmount = shares received, makingAmount = USDC spent
        // SELL: takingAmount = USDC received,   makingAmount = shares given
        let (shares, usdc) = if action.starts_with("SELL") {
            (making, taking) // SELL: making=shares, taking=USDC
        } else {
            (taking, making) // BUY: taking=shares, making=USDC
        };

        let fill_price = if shares > 0.0 {
            (usdc / shares).min(1.0)
        } else {
            price
        };

        let oid = resp.order_id.as_deref().unwrap_or("?");
        println!(
            "[Rust] {} {} FILLED: {:.4} @ {:.4} ${:.2} {}ms",
            action, side, shares, fill_price, usdc, latency
        );

        FillResult {
            filled: true,
            fill_price,
            filled_qty: shares,
            total_cost: usdc,
            latency_ms: latency,
            reason: format!("RUST_{}_FILLED id={}", action, &oid[..oid.len().min(20)]),
        }
    } else if resp
        .success
        .unwrap_or(false)
    {
        // GTC order posted, not yet filled — return order_id for polling
        let oid = resp.order_id.unwrap_or_default();
        println!("[Rust] {} {} POSTED id={} {}ms", action, side, &oid[..oid.len().min(20)], latency);
        FillResult {
            filled: false,
            latency_ms: latency,
            reason: format!("POSTED id={}", oid),
            ..FillResult::default()
        }
    } else {
        let err = resp.error_msg.unwrap_or_default();
        println!("[Rust] {} {} FAILED: {} {}ms", action, side, &err[..err.len().min(100)], latency);
        FillResult {
            filled: false,
            latency_ms: latency,
            reason: format!("FAILED: {}", &err[..err.len().min(100)]),
            ..FillResult::default()
        }
    }
}
