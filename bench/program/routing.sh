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
scratch=$(mktemp -d)
cleanup() {
  for path in routing.rs routing_harness.rs routing-test; do
    if [[ -e "$scratch/$path" || -L "$scratch/$path" ]]; then
      unlink "$scratch/$path"
    fi
  done
  rmdir "$scratch"
}
trap cleanup EXIT
export TMPDIR="$scratch"
cat > "$scratch/routing_harness.rs" <<'RS'
mod config {
    #![allow(dead_code)]
    pub struct Route {
        pub id: String,
        pub host: String,
        pub path_prefix: String,
        pub balance: Balance,
        pub upstreams: Vec<String>,
    }
    pub enum Balance {
        RoundRobin,
    }
}
#[path = "routing.rs"]
mod routing;
RS
ln -s "$PWD/src/routing.rs" "$scratch/routing.rs"
rustc --edition 2021 --test -C opt-level=3 -C strip=symbols "$scratch/routing_harness.rs" -o "$scratch/routing-test"
"$scratch/routing-test" --exact routing::tests::preserves_linear_match_semantics
{
  printf 'layout,routes,repeat,matcher,lookups,elapsed_ns\n'
  "$scratch/routing-test" --ignored --exact routing::tests::lookup_benchmark --nocapture \
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
