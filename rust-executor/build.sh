#!/bin/bash
# Build the Rust executor and prepare for deployment
set -e

echo "=== Building Rust pairbot_executor ==="
export PATH="$HOME/.cargo/bin:$PATH"
cargo build --release

echo "=== Copying .so ==="
cp target/release/libpairbot_executor.so pairbot_executor.so

echo "=== Testing Python import ==="
python3 -c "import sys; sys.path.insert(0,'.'); import pairbot_executor; print('OK:', pairbot_executor.RustExecutor.__doc__ or 'loaded')"

echo ""
echo "=== Build complete ==="
echo "Files to deploy to /opt/pairbot/pair_engine_package/:"
echo "  1. pairbot_executor.so      (Rust shared library)"
echo "  2. rust_live_executor.py    (Python wrapper)"
echo ""
echo "Then in web_bot_multi.py change:"
echo "  from live_executor import LiveExecutor"
echo "  →"
echo "  from rust_live_executor import LiveExecutor"
