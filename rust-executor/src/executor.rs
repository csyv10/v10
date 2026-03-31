//! RustExecutor — the PyO3-exposed executor class.
//!
//! This is the drop-in replacement for Python's LiveExecutor.
//! Python calls: executor.simulate_buy(side, price, qty, ...)
//! Rust handles: CLOB communication, settlement polling, order management.

use crate::clob::ClobClient;
use crate::types::{FillResult, Side};
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
    #[pyo3(signature = (api_key="".to_string(), api_secret="".to_string(), api_passphrase="".to_string(), live=false))]
    fn new(api_key: String, api_secret: String, api_passphrase: String, live: bool) -> Self {
        let runtime = Arc::new(
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(4)
                .enable_all()
                .build()
                .expect("Failed to create Tokio runtime"),
        );

        let client = if live && !api_key.is_empty() {
            let c = ClobClient::new(api_key, api_secret, api_passphrase);
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
