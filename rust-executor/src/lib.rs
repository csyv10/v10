//! PairBot Rust Executor — high-performance order execution for Polymarket CLOB
//!
//! Exposed to Python via PyO3. Drop-in replacement for live_executor.py.
//! Handles: order signing, CLOB API communication, settlement polling, FAK/GTC orders.

mod clob;
mod executor;
mod signing;
mod types;

use pyo3::prelude::*;

/// Python module entry point
#[pymodule]
fn pairbot_executor(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<executor::RustExecutor>()?;
    m.add_class::<types::FillResult>()?;
    Ok(())
}
