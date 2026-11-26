#!/usr/bin/env bash
# Build the production route matcher with the Rust test harness and time lookup only.
set -euo pipefail
if [[ $# != 1 ]]; then
  printf 'Usage: %s OUTPUT_CSV\n' "$0" >&2
  exit 2
fi
output=$1
[[ "$output" = /* ]] || output="$PWD/$output"
cd "$(dirname "$0")/../.."
mkdir -p "$(dirname "$output")"
cargo test --locked --release --bin cauce -- routing::tests::preserves_linear_match_semantics --exact
{
  printf 'layout,routes,repeat,matcher,lookups,elapsed_ns\n'
  cargo test --locked --release --bin cauce -- routing::tests::lookup_benchmark --ignored --exact --nocapture \
    | awk '/^(shared-host|distinct-hosts),/'
} > "$output"
test "$(wc -l < "$output" | tr -d ' ')" = 113
{
  if [[ -n "${CAUCE_SOURCE_REVISION:-}" ]]; then
    printf '%s\n' "$CAUCE_SOURCE_REVISION"
  else
    git rev-parse HEAD
  fi
  uname -smr
  rustc -Vv
  if command -v sha256sum >/dev/null; then
    sha256sum src/routing.rs
  else
    shasum -a 256 src/routing.rs
  fi
} > "${output%.csv}-environment.txt"
