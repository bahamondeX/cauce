#!/usr/bin/env bash
# Execute a checksummed bundle on the target Linux host.
set -Eeuo pipefail
[[ $# == 1 ]]
results=$(realpath -m "$1")
mkdir -p "$results"
cd "$(dirname "$0")/../.."
export PATH="$PWD/bin:$PATH"
sha256sum -c SHA256SUMS >"$results/input-checksums.log"
python3 bench/program/native-host-facts.py >"$results/host-before.json"
sudo -n taskset -c 11 python3 bench/program/native-host-facts.py --hardware-events >"$results/pmu.json"
{
    node --version
    nginx -V
    file bin/cauce bin/cauce-load bin/nginx-dist/nginx
} >"$results/toolchain.log" 2>&1
status=0
timeout -k 15s 85m python3 bench/program/native-first-pass.py bin "$results/first-pass" || status=$?
python3 bench/program/native-host-facts.py >"$results/host-after.json"
printf '%s\n' "$status" >"$results/campaign-exit-status"
if [[ $status == 0 ]]; then
    timeout -k 5s 4m python3 bench/program/replay-transport.py bin/cauce bin/cauce-load \
        "$results/transport" --worker-counts 2 --sample-linux >"$results/transport.log" 2>&1 || true
    if [[ -x bench/program/native-profile-staged.sh ]]; then
        timeout -k 10s 12m bench/program/native-profile-staged.sh "$results/profile" \
            >"$results/profile.log" 2>&1 || true
    fi
fi
exit "$status"
