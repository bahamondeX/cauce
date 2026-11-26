#!/usr/bin/env bash
# Build checksummed Linux artifacts from a committed Git tree.
set -euo pipefail
cd "$(dirname "$0")/.."
root=$(pwd -P)
[[ "$root" == "$(git rev-parse --show-toplevel)" ]]
if [[ $# -lt 1 || $# -gt 4 ]]; then
    echo "usage: $0 OUTPUT [REF [full|core|all [arm64|amd64|all]]]" >&2
    exit 2
fi
release_dir=$(python3 -I -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1")
if [[ "$release_dir" != "$root/"* ]]; then
    echo "release output must be inside the Cauce repository" >&2
    exit 2
fi
mkdir -p "$release_dir" "$root/work"
ref="${2:-HEAD}"
case "${3:-all}" in
    full|core) profiles=("$3") ;;
    all) profiles=(full core) ;;
    *) echo "release profile must be full, core or all" >&2; exit 2 ;;
esac
case "${4:-all}" in
    arm64|amd64) architectures=("$4") ;;
    all) architectures=(arm64 amd64) ;;
    *) echo "release architecture must be arm64, amd64 or all" >&2; exit 2 ;;
esac
commit=$(git rev-parse "$ref^{commit}")
tree=$(git rev-parse "$commit^{tree}")
snapshot=$(mktemp -d "$root/work/cauce-release.XXXXXXXX")
trap 'rm -rf "$snapshot"' EXIT
git archive "$commit" | tar -x -C "$snapshot"
if ! docker info >/dev/null 2>&1; then
    echo "an existing Docker engine is required" >&2
    exit 1
fi
lock_sha=$(shasum -a 256 "$snapshot/Cargo.lock" | awk '{print $1}')
for profile in "${profiles[@]}"; do
    for architecture in "${architectures[@]}"; do
        output="$release_dir/$profile/linux_$architecture"
        mkdir -p "$output"
        if [[ -e "$output/SHA256SUMS" ]]; then
            echo "release artifacts already staged in $output" >&2
            exit 1
        fi
        docker buildx build --platform "linux/$architecture" --target release-artifacts \
            --build-arg "CAUCE_PROFILE=$profile" --output "type=local,dest=$output" "$snapshot"
        (
            cd "$output"
            case "$architecture" in
                arm64) elf_architecture='ARM aarch64' ;;
                amd64) elf_architecture='x86-64' ;;
            esac
            for binary in cauce cauce-collector; do
                test -x "$binary"
                description=$(LC_ALL=C file -b "$binary")
                printf '%s: %s\n' "$binary" "$description" >&2
                [[ "$description" == ELF*"$elf_architecture"* ]]
            done
            printf 'source_commit=%s\nsource_tree=%s\ncargo_lock_sha256=%s\nprofile=%s\nplatform=linux/%s\n' \
                "$commit" "$tree" "$lock_sha" "$profile" "$architecture" > SOURCE
            shasum -a 256 SOURCE cauce cauce-collector > SHA256SUMS
            shasum -a 256 -c SHA256SUMS
        )
    done
done
