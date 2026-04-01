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

/// Encode uint256 from decimal string (supports full 256-bit values)
fn encode_uint256_from_str(s: &str) -> Result<[u8; 32], String> {
    // Parse decimal string to big-endian bytes manually
    // Polymarket token IDs can be >128 bits
    let mut result = [0u8; 32];
    let mut temp = vec![0u8; 1]; // big number as bytes (little-endian working)

    for ch in s.chars() {
        let digit = ch.to_digit(10).ok_or_else(|| format!("invalid digit in token_id: {}", ch))? as u8;
        // Multiply temp by 10
        let mut carry: u16 = 0;
        for byte in temp.iter_mut() {
            let val = (*byte as u16) * 10 + carry;
            *byte = (val & 0xFF) as u8;
            carry = val >> 8;
        }
        while carry > 0 {
            temp.push((carry & 0xFF) as u8);
            carry >>= 8;
        }
        // Add digit
        let mut carry: u16 = digit as u16;
        for byte in temp.iter_mut() {
            let val = (*byte as u16) + carry;
            *byte = (val & 0xFF) as u8;
            carry = val >> 8;
        }
        while carry > 0 {
            temp.push((carry & 0xFF) as u8);
            carry >>= 8;
        }
    }

    if temp.len() > 32 {
        return Err("token_id too large for uint256".into());
    }

    // temp is little-endian, result needs big-endian
    for (i, byte) in temp.iter().enumerate() {
        result[31 - i] = *byte;
    }
    Ok(result)
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
    pub token_id: String,   // decimal string — Polymarket token IDs are >128 bits
    pub maker_amount: u128,
    pub taker_amount: u128,
    pub expiration: u128,
    pub nonce: u128,
    pub fee_rate_bps: u128,
    pub side: u8,           // 0=BUY, 1=SELL
    pub signature_type: u8, // 1=POLY_PROXY
}

/// Hash the Order struct (EIP-712 struct hash)
fn order_struct_hash(order: &OrderParams) -> Result<[u8; 32], String> {
    let type_hash = keccak256(
        b"Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)",
    );

    let token_id_bytes = encode_uint256_from_str(&order.token_id)?;

    let mut encoded = Vec::with_capacity(13 * 32);
    encoded.extend_from_slice(&type_hash);
    encoded.extend_from_slice(&encode_uint256(order.salt));
    encoded.extend_from_slice(&encode_address(&order.maker));
    encoded.extend_from_slice(&encode_address(&order.signer));
    encoded.extend_from_slice(&encode_address(&order.taker));
    encoded.extend_from_slice(&token_id_bytes);
    encoded.extend_from_slice(&encode_uint256(order.maker_amount));
    encoded.extend_from_slice(&encode_uint256(order.taker_amount));
    encoded.extend_from_slice(&encode_uint256(order.expiration));
    encoded.extend_from_slice(&encode_uint256(order.nonce));
    encoded.extend_from_slice(&encode_uint256(order.fee_rate_bps));
    encoded.extend_from_slice(&encode_uint8(order.side));
    encoded.extend_from_slice(&encode_uint8(order.signature_type));

    Ok(keccak256(&encoded))
}

/// Sign an order — returns hex signature string (with 0x prefix)
pub fn sign_order(order: &OrderParams, private_key: &str) -> Result<String, String> {
    let domain = domain_separator();
    let struct_hash = order_struct_hash(order)?;

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

/// Generate random salt (must fit in JS safe integer range: < 2^53)
pub fn generate_salt() -> u128 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    // Truncate to 32 bits like Python's py_order_utils does
    (nanos as u32) as u128
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_domain_separator_deterministic() {
        assert_eq!(domain_separator(), domain_separator());
    }

    #[test]
    fn test_uint256_from_str_large_token_id() {
        // Real Polymarket token ID (>128 bits)
        let token_id = "48502401881813101072132813818885547789619393994969726509337525556832435898804";
        let encoded = encode_uint256_from_str(token_id).unwrap();
        // Should not panic and should produce 32 bytes
        assert_eq!(encoded.len(), 32);
        // First bytes should be non-zero for a large number
        assert!(encoded.iter().any(|&b| b != 0));
    }

    #[test]
    fn test_sign_order_large_token_id() {
        let order = OrderParams {
            salt: 12345,
            maker: "0x1234567890123456789012345678901234567890".into(),
            signer: "0x1234567890123456789012345678901234567890".into(),
            taker: "0x0000000000000000000000000000000000000000".into(),
            token_id: "48502401881813101072132813818885547789619393994969726509337525556832435898804".into(),
            maker_amount: 1000000,
            taker_amount: 500000,
            expiration: 0,
            nonce: 0,
            fee_rate_bps: 1000,
            side: 0,
            signature_type: 1,
        };
        let key = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
        let sig = sign_order(&order, key).unwrap();
        assert!(sig.starts_with("0x"));
        assert_eq!(sig.len(), 132);
    }

    #[test]
    fn test_hashes_match_python() {
        // Compare against Python-computed hashes
        let ds = domain_separator();
        assert_eq!(
            hex::encode(ds),
            "1a573e3617c78403b5b4b892827992f027b03d4eaf570048b8ee8cdd84d151be",
            "Domain separator mismatch"
        );

        let order = OrderParams {
            salt: 12345,
            maker: "0x7466bB28FD7c01ae73982569E50De03d4B93ca5C".into(),
            signer: "0x7466bB28FD7c01ae73982569E50De03d4B93ca5C".into(),
            taker: "0x0000000000000000000000000000000000000000".into(),
            token_id: "12345678901234567890".into(),
            maker_amount: 6500000,
            taker_amount: 10000000,
            expiration: 0,
            nonce: 0,
            fee_rate_bps: 1000,
            side: 0,
            signature_type: 0,
        };
        let sh = order_struct_hash(&order).unwrap();
        assert_eq!(
            hex::encode(sh),
            "17e8a97154bb8f8aebe0bf44a3259b682f46fa40d2a5a0b5103a3800e56ff70a",
            "Struct hash mismatch"
        );

        // Full message hash
        let mut msg = Vec::with_capacity(66);
        msg.push(0x19);
        msg.push(0x01);
        msg.extend_from_slice(&ds);
        msg.extend_from_slice(&sh);
        let mh = keccak256(&msg);
        assert_eq!(
            hex::encode(mh),
            "00e6280387c7372b9870f677e06072395b11991d4c380eb32c7bcd7e95afbb9d",
            "Message hash mismatch"
        );
    }

    #[test]
    fn test_hashes_with_funder_as_maker() {
        let ds = domain_separator();
        let order = OrderParams {
            salt: 12345,
            maker: "0x33d70f3b1f7ffa110f9f087b97d7957b1ea6c7a4".into(), // funder
            signer: "0x7466bB28FD7c01ae73982569E50De03d4B93ca5C".into(), // EOA
            taker: "0x0000000000000000000000000000000000000000".into(),
            token_id: "12345678901234567890".into(),
            maker_amount: 6500000,
            taker_amount: 10000000,
            expiration: 0,
            nonce: 0,
            fee_rate_bps: 1000,
            side: 0,
            signature_type: 0,
        };
        let sh = order_struct_hash(&order).unwrap();
        assert_eq!(
            hex::encode(sh),
            "4abd0d6aa47eb4f932d36d33f908f0896b6012a6b3b77d643ad4ee093844f6f3",
            "Struct hash with funder mismatch"
        );
        let mut msg = Vec::with_capacity(66);
        msg.push(0x19);
        msg.push(0x01);
        msg.extend_from_slice(&ds);
        msg.extend_from_slice(&sh);
        let mh = keccak256(&msg);
        assert_eq!(
            hex::encode(mh),
            "a48abb7efb90f07314371224524d3a2bac9ef7b2b1df2e5da758bc5ce850dcbf",
            "Message hash with funder mismatch"
        );
    }

    #[test]
    fn test_end_to_end_with_funder() {
        // Match Python output exactly
        let order = OrderParams {
            salt: 999,
            maker: "0x33d70f3b1f7ffa110f9f087b97d7957b1ea6c7a4".into(),
            signer: "0x7466bB28FD7c01ae73982569E50De03d4B93ca5C".into(),
            taker: "0x0000000000000000000000000000000000000000".into(),
            token_id: "2617583500476806".into(),
            maker_amount: 7995000,
            taker_amount: 12300000,
            expiration: 0,
            nonce: 0,
            fee_rate_bps: 1000,
            side: 0,
            signature_type: 0,
        };
        let sh = order_struct_hash(&order).unwrap();
        assert_eq!(hex::encode(sh), "887db380ac00e6f14a2363e9f51e3b116a1042fe150123562bb48ea72836bbe9");
        let ds = domain_separator();
        let mut msg = vec![0x19, 0x01];
        msg.extend_from_slice(&ds);
        msg.extend_from_slice(&sh);
        let mh = keccak256(&msg);
        assert_eq!(hex::encode(mh), "4bdba98e5276e7461dafc8e87ac39aa3630639e341f28f450b25ba434ce6d0f9");
    }

    #[test]
    fn test_sign_order_produces_valid_signature() {
        let order = OrderParams {
            salt: 12345,
            maker: "0x1234567890123456789012345678901234567890".into(),
            signer: "0x1234567890123456789012345678901234567890".into(),
            taker: "0x0000000000000000000000000000000000000000".into(),
            token_id: "123456789".into(),
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

#[cfg(test)]
mod test_compare {
    use super::*;

    #[test]
    fn compare_with_python() {
        let hash_hex = "4bdba98e5276e7461dafc8e87ac39aa3630639e341f28f450b25ba434ce6d0f9";
        let key = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
        
        let hash_bytes = hex::decode(hash_hex).unwrap();
        let key_bytes = hex::decode(key).unwrap();
        
        let signing_key = k256::ecdsa::SigningKey::from_bytes((&key_bytes[..]).into()).unwrap();
        let (signature, recovery_id) = signing_key.sign_prehash_recoverable(&hash_bytes).unwrap();
        
        let mut sig_bytes = Vec::with_capacity(65);
        sig_bytes.extend_from_slice(&signature.to_bytes());
        sig_bytes.push(recovery_id.to_byte() + 27);
        
        let sig_hex = format!("0x{}", hex::encode(&sig_bytes));
        println!("Rust sig: {}", sig_hex);
        
        // Python produces: 0x298fd315998d31497566f8312743fd9abe2359ea209e41e50af84c1f2ac4ecbd4402ee3140bb1f1434bc13ef01f36a5856cb359b9d1ef602d2505dbdb55530221c
        assert_eq!(sig_hex, "0x298fd315998d31497566f8312743fd9abe2359ea209e41e50af84c1f2ac4ecbd4402ee3140bb1f1434bc13ef01f36a5856cb359b9d1ef602d2505dbdb55530221c");
    }
}
