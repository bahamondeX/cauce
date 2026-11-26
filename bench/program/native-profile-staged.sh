#!/usr/bin/env bash
# Profile the pinned Cauce workload separately from comparative capacity cells.
set -Eeuo pipefail
[[ $# == 1 ]]
out=$(realpath -m "$1")
mkdir -p "$out"
cd "$(dirname "$0")/../.."
export PATH="$PWD/bin:$PATH"
proxy="$PWD/bin/cauce"
if [[ -x bin/cauce-symbolized ]]; then
    proxy="$PWD/bin/cauce-symbolized"
fi
perf_bin=$(command -v perf || true)
if [[ -z $perf_bin ]] && command -v dnf >/dev/null; then
    sudo -n timeout -k 10s 120s dnf -y install perf >"$out/perf-install.log" 2>&1 || true
    perf_bin=$(command -v perf || true)
fi
run() {
    local name=$1
    shift
    local status=0
    timeout -k 10s 170s python3 bench/program/profile-workload.py "$proxy" bin/cauce-load "$out/$name" \
        --protocol http1 --cpus 0 10 8 --profiler-cpu 11 "$@" >"$out/$name.log" 2>&1 || status=$?
    printf '%s %s\n' "$name" "$status" >>"$out/exit-status.txt"
}
if [[ -n $perf_bin ]]; then
    "$perf_bin" --version >"$out/perf-version.txt"
    run stat --perf "$perf_bin" --sudo --mode stat --seconds 35 --rate 2500 \
        --events cycles,instructions,branches,branch-misses,cache-references,cache-misses,context-switches,cpu-migrations
    run software --perf "$perf_bin" --sudo --mode record --frequency 399 --seconds 60 --rate 2500
    if [[ -f "$out/software/profile/perf.data" ]]; then
        sudo -n chown "$(id -u):$(id -g)" "$out/software/profile/perf.data"
    fi
    run tracepoints --perf "$perf_bin" --sudo --mode stat --seconds 20 --rate 2500 \
        --events raw_syscalls:sys_enter,syscalls:sys_enter_epoll_pwait,syscalls:sys_enter_futex
else
    printf 'perf unavailable\n' >"$out/perf-unavailable.txt"
    run proc --mode proc --seconds 35 --rate 2500
fi
if [[ -x bin/heaptrack ]]; then
    run allocation --logs --heaptrack "$PWD/bin/heaptrack" --seconds 18 --rate 250
    if [[ -x bin/heaptrack-report ]]; then
        for trace in "$out/allocation"/heaptrack.{gz,zst}; do
            if [[ -s $trace ]]; then
                bin/heaptrack-report -f "$trace" -H "$out/allocation/histogram.tsv" \
                    >"$out/allocation/summary.txt" 2>&1 || true
            fi
        done
    fi
fi
