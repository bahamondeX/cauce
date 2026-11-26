#!/usr/bin/env python3
"""Record native Linux role isolation and probe the kernel hardware counters."""

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys


class PerfEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("config", ctypes.c_uint64),
        ("period", ctypes.c_uint64), ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64), ("flags", ctypes.c_uint64),
        ("wakeup_events", ctypes.c_uint32), ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),
    ]


def hardware_events():
    syscall = {"aarch64": 241, "x86_64": 298}[platform.machine()]
    libc = ctypes.CDLL(None, use_errno=True)
    counters = {}
    for name, kind, event in (
        ("cycles", 0, 0), ("instructions", 0, 1), ("cache-references", 0, 2),
        ("cache-misses", 0, 3), ("branches", 0, 4), ("branch-misses", 0, 5),
        ("L1-dcache-load-misses", 3, 1 << 16),
        ("LLC-load-misses", 3, 2 | (1 << 16)),
    ):
        attr = PerfEvent(type=kind, size=ctypes.sizeof(PerfEvent), config=event,
                         flags=(1 << 5) | (1 << 6))
        fd = libc.syscall(syscall, ctypes.byref(attr), 0, -1, -1, 0)
        if fd < 0:
            code = ctypes.get_errno()
            counters[name] = {"state": "unavailable", "errno": errno.errorcode.get(code, code)}
            continue
        try:
            sum(range(2_000_000))
            raw = os.read(fd, 8)
            counters[name] = ({"state": "counted", "count": struct.unpack("=Q", raw)[0]}
                              if len(raw) == 8 else {"state": "unavailable", "reason": "short read"})
        except OSError as error:
            counters[name] = {"state": "unavailable", "errno": errno.errorcode.get(error.errno, error.errno)}
        finally:
            os.close(fd)
    return counters


def proc(path):
    try:
        return Path(path).read_text()
    except OSError:
        return None


def qualification():
    machine = platform.machine()
    cpus = set(range(12))
    sibling_sets = {
        i: proc(f"/sys/devices/system/cpu/cpu{i}/topology/thread_siblings_list")
        for i in sorted(cpus)
    }
    available = os.sched_getaffinity(0)
    isolated = all(sibling_sets.values()) and len(set(sibling_sets.values())) == len(cpus)
    quota = proc("/sys/fs/cgroup/cpu.max")
    limited = False
    if quota:
        units, period = quota.split()
        limited = units != "max" and int(units) < len(cpus) * int(period)
    probes = {
        "kernel": platform.uname()._asdict(),
        "lscpu": subprocess.check_output(["lscpu"], text=True),
        "topology": sibling_sets,
        "affinity": sorted(available),
        "roles": {"proxy": list(range(8)), "generator": [8, 9], "backend": [10], "sampler": [11]},
        "cpu_max": quota,
        "scheduler": proc("/proc/stat"),
        "kernel_command_line": proc("/proc/cmdline"),
        "kernel_config": proc(f"/boot/config-{platform.release()}"),
        "loopback": proc("/sys/class/net/lo/mtu"),
        "cpu_frequency_governors": {
            i: proc(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_governor") for i in sorted(cpus)
        },
        "current_frequencies_khz": {
            i: proc(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq") for i in sorted(cpus)
        },
        "perf_sources": sorted(p.name for p in Path("/sys/bus/event_source/devices").glob("*")),
        "isolation_passed": sys.platform == "linux" and machine in ("aarch64", "x86_64")
                            and cpus <= available and isolated and not limited,
    }
    return probes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware-events", action="store_true")
    args = parser.parse_args()
    data = hardware_events() if args.hardware_events else qualification()
    print(json.dumps(data, indent=2))
    return int(not args.hardware_events and not data["isolation_passed"])


if __name__ == "__main__":
    sys.exit(main())
