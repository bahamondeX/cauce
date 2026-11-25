#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_CONFIG="${DOCKER_CONFIG:-$PWD/work/docker}"
export DOCKER_HOST="${CAUCE_DOCKER_HOST:-unix://$HOME/.colima/default/docker.sock}"
if ! docker info >/dev/null 2>&1; then
  colima start
fi
run_dir="work/fuzz6/runs/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$run_dir/corpus" "$run_dir/artifacts"
docker build -f fuzz/Dockerfile -t cauce-h2-fuzz:nightly-2025-10-01 . 2>&1 \
  | tee work/fuzz6/docker-build.log
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
  cauce-h2-fuzz:nightly-2025-10-01 > work/fuzz6/image.txt
docker run --rm --entrypoint sh cauce-h2-fuzz:nightly-2025-10-01 -c \
  'rustc +nightly-2025-10-01 --version; cargo +nightly-2025-10-01 fuzz --version' \
  > work/fuzz6/toolchain.txt 2>&1
docker run --rm \
  -v "$PWD/$run_dir/corpus:/corpus" \
  -v "$PWD/$run_dir/artifacts:/artifacts" \
  cauce-h2-fuzz:nightly-2025-10-01 \
  /corpus -artifact_prefix=/artifacts/ -seed=60616 -runs=100000 -max_total_time=60 -max_len=4096 -timeout=5 -print_coverage=1 2>&1 \
  | tee "$run_dir/campaign.log"
