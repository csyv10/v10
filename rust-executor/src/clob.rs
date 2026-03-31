//! CLOB API client — handles all HTTP communication with Polymarket.
//!
//! Order flow:
//!   1. Build order amounts (maker/taker) from price+size
//!   2. Sign order with EIP-712 (secp256k1)
//!   3. Build L2 HMAC headers (api_secret signs timestamp+method+path+body)
//!   4. POST signed order JSON to /order endpoint

use crate::signing::{generate_salt, sign_order, OrderParams};
use crate::types::{BalanceResponse, OrderResponse, OrderStatus};
use hmac::{Hmac, Mac};
use reqwest::Client;
use serde_json::json;
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

const CLOB_HOST: &str = "https://clob.polymarket.com";
const FEE_RATE_BPS: u128 = 1000;
const ZERO_ADDRESS: &str = "0x0000000000000000000000000000000000000000";
// 1 token = 1_000_000 micro-units in CLOB
const TOKEN_DECIMALS: f64 = 1_000_000.0;

type HmacSha256 = Hmac<Sha256>;

#[derive(Clone, Copy, Debug)]
pub enum OrderType {
    GtcMaker, // post_only=true
    Fak,      // Fill-And-Kill
}

#[derive(Clone, Copy, Debug)]
pub enum OrderSide {
    Buy,  // side=0
    Sell, // side=1
}

pub struct ClobClient {
    http: Client,
    api_key: String,
    api_secret: String,
    api_passphrase: String,
    wallet_address: String, // funder/maker address
    private_key: String,    // for EIP-712 signing
    signer_address: String, // derived from private key
}

impl ClobClient {
    pub fn new(
        api_key: String,
        api_secret: String,
        api_passphrase: String,
        wallet_address: String,
        private_key: String,
    ) -> Self {
        // Derive signer address from private key
        let signer_address = derive_address(&private_key);

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
            wallet_address,
            private_key,
            signer_address,
        }
    }

    /// Build HMAC signature for L2 auth (signs timestamp+method+path+body)
    fn hmac_signature(&self, timestamp: u64, method: &str, path: &str, body: Option<&str>) -> String {
        let secret_bytes = base64::Engine::decode(
            &base64::engine::general_purpose::URL_SAFE,
            &self.api_secret,
        )
        .unwrap_or_default();

        let mut message = format!("{}{}{}", timestamp, method, path);
        if let Some(b) = body {
            message.push_str(b);
        }

        let mut mac = HmacSha256::new_from_slice(&secret_bytes).expect("HMAC key error");
        mac.update(message.as_bytes());
        base64::Engine::encode(
            &base64::engine::general_purpose::URL_SAFE,
            mac.finalize().into_bytes(),
        )
    }

    /// L2 auth headers for GET requests (no body)
    fn get_headers(&self, path: &str) -> Vec<(String, String)> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let sig = self.hmac_signature(timestamp, "GET", path, None);
        vec![
            ("POLY_ADDRESS".into(), self.signer_address.clone()),
            ("POLY_SIGNATURE".into(), sig),
            ("POLY_TIMESTAMP".into(), timestamp.to_string()),
            ("POLY_API_KEY".into(), self.api_key.clone()),
            ("POLY_PASSPHRASE".into(), self.api_passphrase.clone()),
        ]
    }

    /// L2 auth headers for POST requests (with body)
    fn post_headers(&self, path: &str, body: &str) -> Vec<(String, String)> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let sig = self.hmac_signature(timestamp, "POST", path, Some(body));
        vec![
            ("POLY_ADDRESS".into(), self.signer_address.clone()),
            ("POLY_SIGNATURE".into(), sig),
            ("POLY_TIMESTAMP".into(), timestamp.to_string()),
            ("POLY_API_KEY".into(), self.api_key.clone()),
            ("POLY_PASSPHRASE".into(), self.api_passphrase.clone()),
        ]
    }

    /// Authenticated GET
    async fn get_auth(&self, url: &str, path: &str) -> Result<String, String> {
        let mut req = self.http.get(url);
        for (k, v) in self.get_headers(path) {
            req = req.header(&k, &v);
        }
        let resp = req.send().await.map_err(|e| format!("GET {}: {}", path, e))?;
        resp.text().await.map_err(|e| format!("read: {}", e))
    }

    /// Authenticated POST with pre-serialized body
    async fn post_auth(&self, path: &str, body_str: &str) -> Result<String, String> {
        let url = format!("{}{}", CLOB_HOST, path);
        let mut req = self.http.post(&url);
        for (k, v) in self.post_headers(path, body_str) {
            req = req.header(&k, &v);
        }
        req = req.header("Content-Type", "application/json");
        req = req.body(body_str.to_string());

        let resp = req.send().await.map_err(|e| format!("POST {}: {}", path, e))?;
        let status = resp.status();
        let text = resp.text().await.map_err(|e| format!("read: {}", e))?;

        if !status.is_success() {
            return Err(format!("HTTP {}: {}", status, &text[..text.len().min(300)]));
        }
        Ok(text)
    }

    /// Check connectivity
    pub async fn ping(&self) -> Result<(), String> {
        let resp = self
            .http
            .get(format!("{}/time", CLOB_HOST))
            .send()
            .await
            .map_err(|e| format!("ping: {}", e))?;
        if resp.status().is_success() {
            Ok(())
        } else {
            Err(format!("ping status: {}", resp.status()))
        }
    }

    /// Place a signed order
    ///
    /// BUY maker: GtcMaker, post_only=true, 0% fee
    /// SELL TP: GtcMaker, post_only=true, 0% fee
    /// SELL SL: Fak, immediate taker exit
    pub async fn place_order(
        &self,
        token_id: &str,
        price: f64,
        size: f64,
        side: OrderSide,
        order_type: OrderType,
    ) -> Result<OrderResponse, String> {
        let (side_int, side_str) = match side {
            OrderSide::Buy => (0u8, "BUY"),
            OrderSide::Sell => (1u8, "SELL"),
        };
        let (type_str, post_only) = match order_type {
            OrderType::GtcMaker => ("GTC", true),
            OrderType::Fak => ("FAK", false),
        };

        // Calculate maker/taker amounts (mirrors Python get_order_amounts)
        let rounded_price = (price * 100.0).round() / 100.0; // 2 decimal places
        let rounded_size = ((size * 100.0).floor()) / 100.0;  // floor to 2 decimals

        let (maker_amount, taker_amount) = match side {
            OrderSide::Buy => {
                // BUY: maker=USDC, taker=shares
                let taker = (rounded_size * TOKEN_DECIMALS) as u128;
                let maker_raw = rounded_size * rounded_price;
                let maker = (maker_raw * TOKEN_DECIMALS).round() as u128;
                (maker, taker)
            }
            OrderSide::Sell => {
                // SELL: maker=shares, taker=USDC
                let maker = (rounded_size * TOKEN_DECIMALS) as u128;
                let taker_raw = rounded_size * rounded_price;
                let taker = (taker_raw * TOKEN_DECIMALS).round() as u128;
                (maker, taker)
            }
        };

        // Parse token_id to u128
        let token_id_num: u128 = token_id.parse().map_err(|e| format!("bad token_id: {}", e))?;

        // Build and sign the order
        let salt = generate_salt();
        let order_params = OrderParams {
            salt,
            maker: self.wallet_address.clone(),
            signer: self.signer_address.clone(),
            taker: ZERO_ADDRESS.into(),
            token_id: token_id_num,
            maker_amount,
            taker_amount,
            expiration: 0,
            nonce: 0,
            fee_rate_bps: FEE_RATE_BPS,
            side: side_int,
            signature_type: 1, // POLY_PROXY
        };

        let signature = sign_order(&order_params, &self.private_key)?;

        // Build the order JSON (matches Python order.dict() format)
        let order_json = json!({
            "salt": salt.to_string(),
            "maker": &self.wallet_address,
            "signer": &self.signer_address,
            "taker": ZERO_ADDRESS,
            "tokenId": token_id,
            "makerAmount": maker_amount.to_string(),
            "takerAmount": taker_amount.to_string(),
            "expiration": "0",
            "nonce": "0",
            "feeRateBps": FEE_RATE_BPS.to_string(),
            "side": side_str,
            "signatureType": 1,
            "signature": signature,
        });

        let body = json!({
            "order": order_json,
            "owner": &self.api_key,
            "orderType": type_str,
            "postOnly": post_only,
        });

        // Serialize deterministically (no spaces, like Python separators=(",",":"))
        let body_str = serde_json::to_string(&body).map_err(|e| format!("serialize: {}", e))?;

        println!(
            "[Rust] {} {} {} @ {:.4} size={:.2} type={}",
            side_str,
            if post_only { "maker" } else { "taker" },
            &token_id[..16.min(token_id.len())],
            price,
            size,
            type_str,
        );

        // POST to CLOB
        let resp_text = self.post_auth("/order", &body_str).await?;

        // Parse response
        serde_json::from_str::<OrderResponse>(&resp_text)
            .map_err(|e| format!("parse response: {} body={}", e, &resp_text[..resp_text.len().min(200)]))
    }

    /// Update balance allowance
    pub async fn update_balance_allowance(&self, token_id: &str, sig_type: u8) -> Result<(), String> {
        let path = "/balance-allowance/update";
        let url = format!(
            "{}{}?asset_type=CONDITIONAL&token_id={}&signature_type={}",
            CLOB_HOST, path, token_id, sig_type
        );
        self.get_auth(&url, path).await?;
        Ok(())
    }

    /// Get balance
    pub async fn get_balance(&self, token_id: &str, sig_type: u8) -> Result<f64, String> {
        let path = "/balance-allowance";
        let url = format!(
            "{}{}?asset_type=CONDITIONAL&token_id={}&signature_type={}",
            CLOB_HOST, path, token_id, sig_type
        );
        let body = self.get_auth(&url, path).await?;
        let resp: BalanceResponse =
            serde_json::from_str(&body).map_err(|e| format!("parse balance: {}", e))?;
        Ok(resp.shares())
    }

    /// Get order status
    pub async fn get_order(&self, order_id: &str) -> Result<OrderStatus, String> {
        let path = format!("/data/order/{}", order_id);
        let url = format!("{}{}", CLOB_HOST, path);
        let body = self.get_auth(&url, &path).await?;
        serde_json::from_str(&body).map_err(|e| format!("parse order: {}", e))
    }

    /// Cancel order
    pub async fn cancel_order(&self, order_id: &str) -> Result<(), String> {
        let url = format!("{}/data/order/{}", CLOB_HOST, order_id);
        self.http
            .delete(&url)
            .send()
            .await
            .map_err(|e| format!("cancel: {}", e))?;
        Ok(())
    }

    /// Get orderbook
    pub async fn get_orderbook(&self, token_id: &str) -> Result<serde_json::Value, String> {
        let url = format!("{}/book?token_id={}", CLOB_HOST, token_id);
        let body = self
            .http
            .get(&url)
            .send()
            .await
            .map_err(|e| format!("orderbook: {}", e))?
            .text()
            .await
            .map_err(|e| format!("read: {}", e))?;
        serde_json::from_str(&body).map_err(|e| format!("parse: {}", e))
    }
}

/// Derive Ethereum address from private key
fn derive_address(private_key: &str) -> String {
    let key_hex = private_key.strip_prefix("0x").unwrap_or(private_key);
    let key_bytes = hex::decode(key_hex).unwrap_or_default();

    if key_bytes.len() != 32 {
        return String::new();
    }

    let signing_key = k256::ecdsa::SigningKey::from_bytes((&key_bytes[..]).into())
        .expect("invalid private key");
    let verifying_key = signing_key.verifying_key();
    let public_key = verifying_key.to_encoded_point(false);
    let public_bytes = &public_key.as_bytes()[1..]; // skip 0x04 prefix

    use sha3::{Digest, Keccak256};
    let hash = Keccak256::digest(public_bytes);
    format!("0x{}", hex::encode(&hash[12..]))
}
