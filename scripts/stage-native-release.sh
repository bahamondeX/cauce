#!/usr/bin/env bash
# Build checksummed Linux artifacts from a committed Git tree.
set -euo pipefail
cd "$(dirname "$0")/.."
release_dir=$(mkdir -p "$1" && cd "$1" && pwd -P)
ref="${2:-HEAD}"
commit=$(git rev-parse "$ref^{commit}")
tree=$(git rev-parse "$commit^{tree}")
snapshot=$(mktemp -d "${TMPDIR:-/tmp}/cauce-release.XXXXXXXX")
trap 'rm -rf "$snapshot"' EXIT
git archive "$commit" | tar -x -C "$snapshot"
if ! docker info >/dev/null 2>&1; then
    colima start
fi
for profile in full core; do
    output="$release_dir/$profile"
    mkdir -p "$output"
    docker buildx build --platform linux/amd64,linux/arm64 --target release-artifacts \
        --build-arg "CAUCE_PROFILE=$profile" --output "type=local,dest=$output" "$snapshot"
done
(
    cd "$release_dir"
    printf 'source_commit=%s\nsource_tree=%s\n' "$commit" "$tree" > SOURCE
    shasum -a 256 "$snapshot/Cargo.lock" | awk '{print "cargo_lock_sha256=" $1}' >> SOURCE
    find full core -type f -name 'cauce*' | LC_ALL=C sort | while IFS= read -r binary; do
        file "$binary" >&2
        shasum -a 256 "$binary"
    done > SHA256SUMS
    shasum -a 256 -c SHA256SUMS
)
