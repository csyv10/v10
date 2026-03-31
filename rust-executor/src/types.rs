//! Shared types — mirrors Python FillResult and order structs.

use pyo3::prelude::*;
use serde::{Deserialize, Serialize};

/// Result of an order execution — returned to Python.
#[pyclass]
#[derive(Clone, Debug)]
pub struct FillResult {
    #[pyo3(get, set)]
    pub filled: bool,
    #[pyo3(get, set)]
    pub fill_price: f64,
    #[pyo3(get, set)]
    pub filled_qty: f64,
    #[pyo3(get, set)]
    pub total_cost: f64,
    #[pyo3(get, set)]
    pub latency_ms: f64,
    #[pyo3(get, set)]
    pub reason: String,
}

impl FillResult {
    pub fn default() -> Self {
        Self {
            filled: false,
            fill_price: 0.0,
            filled_qty: 0.0,
            total_cost: 0.0,
            latency_ms: 0.0,
            reason: String::new(),
        }
    }
}

#[pymethods]
impl FillResult {
    #[new]
    fn new() -> Self {
        Self {
            filled: false,
            fill_price: 0.0,
            filled_qty: 0.0,
            total_cost: 0.0,
            latency_ms: 0.0,
            reason: String::new(),
        }
    }

    fn __repr__(&self) -> String {
        format!(
            "FillResult(filled={}, price={:.4}, qty={:.2}, cost={:.2}, reason={})",
            self.filled, self.fill_price, self.filled_qty, self.total_cost, self.reason
        )
    }
}

/// CLOB API response for order placement
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OrderResponse {
    pub error_msg: Option<String>,
    #[serde(alias = "orderID")]
    pub order_id: Option<String>,
    pub taking_amount: Option<String>,
    pub making_amount: Option<String>,
    pub status: Option<String>,
    pub success: Option<bool>,
}

/// CLOB API response for order status polling
#[derive(Debug, Deserialize)]
pub struct OrderStatus {
    pub status: Option<String>,
    pub size_matched: Option<String>,
    #[serde(alias = "sizeMatched")]
    pub size_matched_alt: Option<String>,
    pub avg_price: Option<String>,
    pub price: Option<String>,
}

impl OrderStatus {
    pub fn matched_qty(&self) -> f64 {
        self.size_matched
            .as_ref()
            .or(self.size_matched_alt.as_ref())
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.0)
    }

    pub fn fill_price(&self, default: f64) -> f64 {
        self.avg_price
            .as_ref()
            .or(self.price.as_ref())
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(default)
    }

    pub fn is_matched(&self) -> bool {
        self.status.as_deref() == Some("matched")
    }

    pub fn is_cancelled(&self) -> bool {
        matches!(
            self.status.as_deref(),
            Some("cancelled") | Some("unmatched")
        )
    }
}

/// Balance/allowance response from CLOB
#[derive(Debug, Deserialize)]
pub struct BalanceResponse {
    pub balance: Option<String>,
}

impl BalanceResponse {
    /// Balance in shares (CLOB returns micro-units)
    pub fn shares(&self) -> f64 {
        self.balance
            .as_ref()
            .and_then(|s| s.parse::<i64>().ok())
            .map(|v| v as f64 / 1_000_000.0)
            .unwrap_or(0.0)
    }
}

/// Side of a trade
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Up,
    Down,
}

impl Side {
    pub fn as_str(&self) -> &'static str {
        match self {
            Side::Up => "UP",
            Side::Down => "DOWN",
        }
    }

    pub fn from_str(s: &str) -> Option<Self> {
        match s.to_uppercase().as_str() {
            "UP" => Some(Side::Up),
            "DOWN" => Some(Side::Down),
            _ => None,
        }
    }
}
