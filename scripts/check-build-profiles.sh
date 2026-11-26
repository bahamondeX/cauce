#!/usr/bin/env bash
# Compile each distribution and validate its config acceptance boundary.
set -euo pipefail
cd "$(dirname "$0")/.."
target_dir="${CARGO_TARGET_DIR:-target}"
for profile in core web network edge application full; do
    cargo build --locked --bin cauce --no-default-features --features "$profile"
    proxy="$target_dir/debug/cauce"
    echo "profile $profile"
    "$proxy" capabilities --json
    "$proxy" check examples/cauce.yaml
    unsupported() {
        local output
        if output=$("$proxy" check "$1" 2>&1); then
            echo "unexpected acceptance: $1" >&2
            exit 1
        fi
        if [[ "$output" != *"requires the $2 build feature"* ]]; then
            echo "$output" >&2
            exit 1
        fi
    }
    case "$profile" in
        web|edge|full)
            CAUCE_DOCUMENT_ROOT="$(pwd -P)/examples" "$proxy" check examples/content-and-cache.yaml ;;
        *) CAUCE_DOCUMENT_ROOT="$(pwd -P)/examples" unsupported examples/content-and-cache.yaml web ;;
    esac
    case "$profile" in
        network|full) "$proxy" check examples/stream.yaml ;;
        *) unsupported examples/stream.yaml stream ;;
    esac
    case "$profile" in
        edge|full) "$proxy" check examples/http3.yaml ;;
        *) unsupported examples/http3.yaml quic ;;
    esac
    case "$profile" in
        application|full)
            "$proxy" check examples/fastcgi.yaml
            "$proxy" check examples/wsgi.yaml ;;
        *) unsupported examples/fastcgi.yaml application ;;
    esac
    case "$profile" in
        full) : ;;
        *) unsupported examples/mail.yaml mail ;;
    esac
done
