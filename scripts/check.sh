#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
root=$(pwd -P)
[ "$root" = "$(git rev-parse --show-toplevel)" ]
scope="$root/work/check-environment"
python=$(command -v python3)
scope=$("$python" -IB -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$scope")
case "$scope" in "$root"/*) ;; *) exit 1 ;; esac
export TMPDIR="$scope/tmp" CARGO_HOME="$scope/cargo" CARGO_TARGET_DIR="$scope/target"
export UV_CACHE_DIR="$scope/uv-cache" UV_PROJECT_ENVIRONMENT="$scope/venv"
export UV_PYTHON_INSTALL_DIR="$scope/interpreters" UV_PYTHON_DOWNLOADS=never
mkdir -p "$TMPDIR"
uv sync --locked --project "$root/tests/compatibility" --python "$python"
python="$UV_PROJECT_ENVIRONMENT/bin/python"
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked
CAUCE_BIN="$CARGO_TARGET_DIR/debug/cauce" "$python" -EB "$root/tests/integration.py"
"$python" -IB "$root/scripts/test-collector.py" --binary "$CARGO_TARGET_DIR/debug/cauce-collector" --report "$scope/collector.json"
"$python" -IB "$root/scripts/test-collector-storage.py" --binary "$CARGO_TARGET_DIR/debug/cauce-collector" --report "$scope/collector-storage.json"
