#!/bin/sh
set -eu

if [ "$(uname -s)" != Linux ]; then
    echo "This manifest requires Linux" >&2
    exit 2
fi

uname -srmv
lscpu
printf '\nVirtualization\n'
if command -v systemd-detect-virt >/dev/null 2>&1; then
    systemd-detect-virt || true
fi
printf '\nProcess affinity and cgroup\n'
taskset -pc "$$"
cat /proc/self/cgroup
printf '\nClock source and perf access\n'
cat /sys/devices/system/clocksource/clocksource0/current_clocksource
cat /proc/sys/kernel/perf_event_paranoid
ls /sys/bus/event_source/devices
printf '\nHost pressure\n'
for kind in cpu memory io; do
    if [ -r "/proc/pressure/$kind" ]; then
        printf '%s\n' "$kind"
        cat "/proc/pressure/$kind"
    fi
done
printf '\nCgroup limits\n'
cgroup=$(sed -n 's|^0::||p' /proc/self/cgroup)
for control in cpu.max cpuset.cpus.effective memory.max pids.max; do
    path="/sys/fs/cgroup$cgroup/$control"
    if [ -r "$path" ]; then
        printf '%s ' "$control"
        cat "$path"
    fi
done
printf '\nToolchain and runtime\n'
getconf GNU_LIBC_VERSION || true
for tool in rustc cargo node perf; do
    if command -v "$tool" >/dev/null 2>&1; then
        "$tool" --version || true
    fi
done
if command -v docker >/dev/null 2>&1; then
    docker version --format '{{.Server.Version}} {{.Server.Arch}}' || true
    docker compose version --short || true
fi
if [ "$#" -gt 0 ]; then
    sha256sum "$@"
fi
