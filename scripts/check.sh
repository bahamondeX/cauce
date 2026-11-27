#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
root=$(pwd -P)
[ "$root" = "$(git rev-parse --show-toplevel)" ]
scope="$root/work/check-environment"
python=$(command -v python3)
local_path() {
    resolved=$("$python" -IB -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1")
    case "$resolved" in "$root"/*) printf '%s\n' "$resolved" ;; *) return 1 ;; esac
}
scope=$(local_path "$scope")
TMPDIR=$(local_path "$scope/tmp")
CARGO_HOME=$(local_path "$scope/cargo")
CARGO_TARGET_DIR=$(local_path "$scope/target")
UV_CACHE_DIR=$(local_path "$scope/uv-cache")
UV_PROJECT_ENVIRONMENT=$(local_path "$scope/venv")
UV_PYTHON_INSTALL_DIR=$(local_path "$scope/interpreters")
export TMPDIR CARGO_HOME CARGO_TARGET_DIR UV_CACHE_DIR UV_PROJECT_ENVIRONMENT UV_PYTHON_INSTALL_DIR
export UV_PYTHON_DOWNLOADS=never
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
