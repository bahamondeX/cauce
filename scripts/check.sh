#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked
uv run --no-project --with jsonschema python tests/integration.py
python3 scripts/test-collector.py
python3 scripts/test-collector-storage.py
