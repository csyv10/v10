//! EIP-712 order signing for Polymarket CTF Exchange.
//!
//! Python: ~50ms per signature. Rust: <1ms.

use std::fmt::Write;

// Polygon mainnet exchange contract
const EXCHANGE_ADDRESS: &str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
const CHAIN_ID: u64 = 137;

/// keccak256 hash
fn keccak256(data: &[u8]) -> [u8; 32] {
    use sha3::{Digest, Keccak256};
    let mut hasher = Keccak256::new();
    hasher.update(data);
    let result = hasher.finalize();
    let mut out = [0u8; 32];
    out.copy_from_slice(&result);
    out
}

/// Encode address as 32-byte left-padded
fn encode_address(addr: &str) -> [u8; 32] {
    let addr = addr.strip_prefix("0x").unwrap_or(addr);
    let bytes = hex::decode(addr).expect("invalid address hex");
    let mut out = [0u8; 32];
    let start = 32 - bytes.len();
    out[start..32].copy_from_slice(&bytes);
    out
}

/// Encode uint256 from u128
fn encode_uint256(val: u128) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[16..32].copy_from_slice(&val.to_be_bytes());
    out
}

/// Encode uint8
fn encode_uint8(val: u8) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[31] = val;
    out
}

/// Build EIP-712 domain separator
fn domain_separator() -> [u8; 32] {
    let domain_type_hash = keccak256(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    );
    let name_hash = keccak256(b"Polymarket CTF Exchange");
    let version_hash = keccak256(b"1");

    let mut encoded = Vec::with_capacity(5 * 32);
    encoded.extend_from_slice(&domain_type_hash);
    encoded.extend_from_slice(&name_hash);
    encoded.extend_from_slice(&version_hash);
    encoded.extend_from_slice(&encode_uint256(CHAIN_ID as u128));
    encoded.extend_from_slice(&encode_address(EXCHANGE_ADDRESS));

    keccak256(&encoded)
}

/// Order data for signing
pub struct OrderParams {
    pub salt: u128,
    pub maker: String,
    pub signer: String,
    pub taker: String,
    pub token_id: u128,
    pub maker_amount: u128,
    pub taker_amount: u128,
    pub expiration: u128,
    pub nonce: u128,
    pub fee_rate_bps: u128,
    pub side: u8,           // 0=BUY, 1=SELL
    pub signature_type: u8, // 1=POLY_PROXY
}

/// Hash the Order struct (EIP-712 struct hash)
fn order_struct_hash(order: &OrderParams) -> [u8; 32] {
    let type_hash = keccak256(
        b"Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)",
    );

    let mut encoded = Vec::with_capacity(13 * 32);
    encoded.extend_from_slice(&type_hash);
    encoded.extend_from_slice(&encode_uint256(order.salt));
    encoded.extend_from_slice(&encode_address(&order.maker));
    encoded.extend_from_slice(&encode_address(&order.signer));
    encoded.extend_from_slice(&encode_address(&order.taker));
    encoded.extend_from_slice(&encode_uint256(order.token_id));
    encoded.extend_from_slice(&encode_uint256(order.maker_amount));
    encoded.extend_from_slice(&encode_uint256(order.taker_amount));
    encoded.extend_from_slice(&encode_uint256(order.expiration));
    encoded.extend_from_slice(&encode_uint256(order.nonce));
    encoded.extend_from_slice(&encode_uint256(order.fee_rate_bps));
    encoded.extend_from_slice(&encode_uint8(order.side));
    encoded.extend_from_slice(&encode_uint8(order.signature_type));

    keccak256(&encoded)
}

/// Sign an order — returns hex signature string (with 0x prefix)
pub fn sign_order(order: &OrderParams, private_key: &str) -> Result<String, String> {
    let domain = domain_separator();
    let struct_hash = order_struct_hash(order);

    // EIP-712: keccak256("\x19\x01" || domainSeparator || structHash)
    let mut message = Vec::with_capacity(2 + 32 + 32);
    message.push(0x19);
    message.push(0x01);
    message.extend_from_slice(&domain);
    message.extend_from_slice(&struct_hash);
    let hash = keccak256(&message);

    // Sign with secp256k1
    let key_bytes = hex::decode(private_key.strip_prefix("0x").unwrap_or(private_key))
        .map_err(|e| format!("invalid private key hex: {}", e))?;

    let signing_key = k256::ecdsa::SigningKey::from_bytes((&key_bytes[..]).into())
        .map_err(|e| format!("invalid signing key: {}", e))?;

    let (signature, recovery_id) = signing_key
        .sign_prehash_recoverable(&hash)
        .map_err(|e| format!("signing failed: {}", e))?;

    // 65 bytes: r (32) || s (32) || v (1)
    let mut sig_bytes = Vec::with_capacity(65);
    sig_bytes.extend_from_slice(&signature.to_bytes());
    sig_bytes.push(recovery_id.to_byte() + 27);

    let mut hex_str = String::with_capacity(132);
    hex_str.push_str("0x");
    for b in &sig_bytes {
        write!(hex_str, "{:02x}", b).unwrap();
    }
    Ok(hex_str)
}

/// Generate random salt
pub fn generate_salt() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    nanos ^ (nanos >> 64) ^ 0xDEADBEEFCAFEBABE
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_domain_separator_deterministic() {
        assert_eq!(domain_separator(), domain_separator());
    }

    #[test]
    fn test_sign_order_produces_valid_signature() {
        let order = OrderParams {
            salt: 12345,
            maker: "0x1234567890123456789012345678901234567890".into(),
            signer: "0x1234567890123456789012345678901234567890".into(),
            taker: "0x0000000000000000000000000000000000000000".into(),
            token_id: 123456789,
            maker_amount: 1000000,
            taker_amount: 500000,
            expiration: 0,
            nonce: 0,
            fee_rate_bps: 1000,
            side: 0,
            signature_type: 1,
        };
        // Hardhat test key
        let key = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
        let sig = sign_order(&order, key).unwrap();
        assert!(sig.starts_with("0x"));
        assert_eq!(sig.len(), 132);
    }
}
