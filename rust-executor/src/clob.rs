//! CLOB API client — handles all HTTP communication with Polymarket.
//!
//! Replaces py_clob_client with direct reqwest calls for maximum speed.

use crate::types::{BalanceResponse, OrderResponse, OrderStatus};
use hmac::{Hmac, Mac};
use reqwest::Client;
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

const CLOB_HOST: &str = "https://clob.polymarket.com";

type HmacSha256 = Hmac<Sha256>;

/// Polymarket CLOB API client
pub struct ClobClient {
    http: Client,
    api_key: String,
    api_secret: String,
    api_passphrase: String,
}

impl ClobClient {
    pub fn new(api_key: String, api_secret: String, api_passphrase: String) -> Self {
        let http = Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .pool_max_idle_per_host(10)
            .build()
            .expect("Failed to build HTTP client");

        Self {
            http,
            api_key,
            api_secret,
            api_passphrase,
        }
    }

    /// Generate L2 auth headers for CLOB API
    fn auth_headers(&self, method: &str, path: &str) -> Vec<(String, String)> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string();

        let message = format!("{}{}{}", timestamp, method.to_uppercase(), path);

        let mut mac =
            HmacSha256::new_from_slice(self.api_secret.as_bytes()).expect("HMAC key error");
        mac.update(message.as_bytes());
        let signature = base64::Engine::encode(
            &base64::engine::general_purpose::STANDARD,
            mac.finalize().into_bytes(),
        );

        vec![
            ("POLY-ADDRESS".into(), self.api_key.clone()),
            ("POLY-SIGNATURE".into(), signature),
            ("POLY-TIMESTAMP".into(), timestamp),
            ("POLY-PASSPHRASE".into(), self.api_passphrase.clone()),
        ]
    }

    /// GET with L2 auth
    async fn get_authenticated(&self, url: &str, path: &str) -> reqwest::Result<String> {
        let mut req = self.http.get(url);
        for (k, v) in self.auth_headers("GET", path) {
            req = req.header(&k, &v);
        }
        let resp = req.send().await?;
        resp.text().await
    }

    /// Check CLOB connectivity
    pub async fn ping(&self) -> Result<(), String> {
        let resp = self
            .http
            .get(format!("{}/time", CLOB_HOST))
            .send()
            .await
            .map_err(|e| format!("CLOB ping failed: {}", e))?;
        if resp.status().is_success() {
            Ok(())
        } else {
            Err(format!("CLOB ping status: {}", resp.status()))
        }
    }

    /// Update balance allowance — triggers CLOB to re-scan chain
    pub async fn update_balance_allowance(
        &self,
        token_id: &str,
        signature_type: u8,
    ) -> Result<(), String> {
        let path = "/balance-allowance/update";
        let url = format!(
            "{}{}?asset_type=CONDITIONAL&token_id={}&signature_type={}",
            CLOB_HOST, path, token_id, signature_type
        );
        self.get_authenticated(&url, path)
            .await
            .map_err(|e| format!("update_balance_allowance: {}", e))?;
        Ok(())
    }

    /// Get current balance for a token
    pub async fn get_balance(&self, token_id: &str, signature_type: u8) -> Result<f64, String> {
        let path = "/balance-allowance";
        let url = format!(
            "{}{}?asset_type=CONDITIONAL&token_id={}&signature_type={}",
            CLOB_HOST, path, token_id, signature_type
        );
        let body = self
            .get_authenticated(&url, path)
            .await
            .map_err(|e| format!("get_balance: {}", e))?;
        let resp: BalanceResponse =
            serde_json::from_str(&body).map_err(|e| format!("parse balance: {}", e))?;
        Ok(resp.shares())
    }

    /// Get order status by ID
    pub async fn get_order(&self, order_id: &str) -> Result<OrderStatus, String> {
        let path = format!("/data/order/{}", order_id);
        let url = format!("{}{}", CLOB_HOST, path);
        let body = self
            .get_authenticated(&url, &path)
            .await
            .map_err(|e| format!("get_order: {}", e))?;
        serde_json::from_str(&body).map_err(|e| format!("parse order: {}", e))
    }

    /// Cancel an order
    pub async fn cancel_order(&self, order_id: &str) -> Result<(), String> {
        let url = format!("{}/data/order/{}", CLOB_HOST, order_id);
        self.http
            .delete(&url)
            .send()
            .await
            .map_err(|e| format!("cancel: {}", e))?;
        Ok(())
    }
}
