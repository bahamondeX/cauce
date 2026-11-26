#!/usr/bin/env bash
# Stage an ELF build using explicitly supplied, preinstalled Rust and Zig.
set -euo pipefail
cd "$(dirname "$0")/.."
root=$(pwd -P)
[[ "$root" == "$(git rev-parse --show-toplevel)" ]]
if [[ $# != 6 ]]; then
    echo "usage: $0 OUTPUT REF arm64|amd64 full|core RUST_BIN_DIR ZIG_BINARY" >&2
    exit 2
fi
inside() {
    local resolved
    resolved=$(python3 -IB -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1")
    if [[ "$resolved" != "$root/"* ]]; then
        echo "all build outputs and caches must remain in Cauce" >&2
        return 2
    fi
    printf '%s\n' "$resolved"
}
destination=$(inside "$1")
case "$3" in arm64) cpu=aarch64 ;; amd64) cpu=x86_64 ;; *) exit 2 ;; esac
case "$4" in full|core) profile=$4 ;; *) exit 2 ;; esac
[[ -x "$5/cargo" && -x "$5/rustc" && -x "$6" && ! -e "$destination/SHA256SUMS" ]]
zig=$(python3 -IB -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$6")
rust_bin=$(cd "$5" && pwd -P)
export PATH="$rust_bin:$PATH" RUSTC="$rust_bin/rustc"
unset RUSTC_WRAPPER RUSTC_WORKSPACE_WRAPPER
CARGO_HOME=$(inside "${CARGO_HOME:-$root/work/zig-release/cargo}")
CARGO_TARGET_DIR=$(inside "${CARGO_TARGET_DIR:-$root/work/zig-release/$cpu}")
ZIG_GLOBAL_CACHE_DIR=$(inside "${ZIG_GLOBAL_CACHE_DIR:-$root/work/zig-release/zig-cache}")
ZIG_LOCAL_CACHE_DIR=$(inside "${ZIG_LOCAL_CACHE_DIR:-$root/work/zig-release/zig-local-$cpu}")
TMPDIR=$(inside "${CAUCE_STAGE_TMPDIR:-$root/work/zig-release/tmp}")
export CARGO_HOME CARGO_TARGET_DIR ZIG_GLOBAL_CACHE_DIR ZIG_LOCAL_CACHE_DIR TMPDIR
mkdir -p "$TMPDIR" "$destination" "$CARGO_TARGET_DIR/.cauce-stage-tools"
stage=$(mktemp -d "$TMPDIR/cauce-zig.XXXXXXXX")
trap 'rm -rf "$stage"' EXIT
commit=$(git rev-parse "$2^{commit}")
tree=$(git rev-parse "$commit^{tree}")
mkdir "$stage/source"
git archive "$commit" | tar -xf - -C "$stage/source"
target="$cpu-unknown-linux-gnu"
tools="$CARGO_TARGET_DIR/.cauce-stage-tools"
printf '#!/bin/bash\nexec %q cc -target %s-linux-gnu.2.36 "$@"\n' "$zig" "$cpu" > "$tools/cc"
printf '#!/bin/bash\nexec %q ar "$@"\n' "$zig" > "$tools/ar"
chmod +x "$tools/cc" "$tools/ar"
export CRATE_CC_NO_DEFAULTS=1
export "CC_${cpu}_unknown_linux_gnu=$tools/cc"
export "AR_${cpu}_unknown_linux_gnu=$tools/ar"
export "CFLAGS_${cpu}_unknown_linux_gnu=-O3 -fPIC -ffunction-sections -fdata-sections"
linker_key=$(printf 'CARGO_TARGET_%s_UNKNOWN_LINUX_GNU_LINKER' "$cpu" | tr '[:lower:]' '[:upper:]')
export "$linker_key=$tools/cc"
cd "$stage/source"
cargo build --locked --release --no-default-features --features "$profile" --target "$target"
lock=$(shasum -a 256 Cargo.lock | awk '{print $1}')
for binary in cauce cauce-collector; do
    built="$CARGO_TARGET_DIR/$target/release/$binary"
    description=$(LC_ALL=C file -b "$built")
    if [[ "$cpu" == aarch64 ]]; then
        [[ "$description" == ELF*'ARM aarch64'* ]]
    else
        [[ "$description" == ELF*'x86-64'* ]]
    fi
    cp "$built" "$destination/$binary"
done
cd "$destination"
printf 'source_commit=%s\nsource_tree=%s\ncargo_lock_sha256=%s\nprofile=%s\nplatform=linux/%s\nglibc_target=2.36\nrustc=%s\nzig=%s\nzig_sha256=%s\n' \
    "$commit" "$tree" "$lock" "$profile" "$3" "$(rustc --version)" "$("$zig" version)" "$(shasum -a 256 "$zig" | awk '{print $1}')" > SOURCE
shasum -a 256 SOURCE cauce cauce-collector > SHA256SUMS
shasum -a 256 -c SHA256SUMS
