//! EIP-712 order signing for Polymarket CLOB.
//!
//! This is the most CPU-intensive part of order execution.
//! Python: ~50ms per signature. Rust: <1ms.

use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

/// CLOB L2 auth header generation
pub struct L2Auth {
    api_key: String,
    api_secret: String,
    api_passphrase: String,
}

impl L2Auth {
    pub fn new(api_key: String, api_secret: String, api_passphrase: String) -> Self {
        Self {
            api_key,
            api_secret,
            api_passphrase,
        }
    }

    /// Generate auth headers for a CLOB API request
    pub fn headers(&self, method: &str, path: &str) -> Vec<(&'static str, String)> {
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
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
            ("POLY-ADDRESS", self.api_key.clone()),
            ("POLY-SIGNATURE", signature),
            ("POLY-TIMESTAMP", timestamp),
            ("POLY-PASSPHRASE", self.api_passphrase.clone()),
        ]
    }
}
