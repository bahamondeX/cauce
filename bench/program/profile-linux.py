#!/usr/bin/env python3
"""Capture Linux process state and optional perf samples for one local proxy PID."""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time


def run(command, path):
    try:
        with path.open("w") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        return result.returncode
    except OSError as error:
        path.write_text(str(error) + "\n")
        return None


def snapshot(pid, path):
    path.mkdir()
    proc = Path(f"/proc/{pid}")
    for name in ("stat", "status", "smaps_rollup", "schedstat", "io", "cgroup"):
        try:
            (path / name).write_text((proc / name).read_text())
        except OSError as error:
            (path / name).write_text(f"unavailable: {error}\n")
    threads = {}
    for thread in sorted((proc / "task").iterdir()):
        try:
            threads[thread.name] = {
                name: (thread / name).read_text()
                for name in ("comm", "stat", "schedstat", "status")
            }
        except OSError as error:
            threads[thread.name] = {"error": str(error)}
    (path / "threads.json").write_text(json.dumps(threads, indent=2) + "\n")
    cgroup = next((line[3:] for line in (path / "cgroup").read_text().splitlines()
                   if line.startswith("0::")), None)
    if cgroup is not None:
        for name in ("cpu.stat", "memory.current", "memory.peak", "cpu.pressure"):
            source = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / name
            try:
                (path / name).write_text(source.read_text())
            except OSError as error:
                (path / name).write_text(f"unavailable: {error}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--perf", default="perf")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--events", default="task-clock,context-switches,cpu-migrations,page-faults,cycles,"
                        "instructions,branches,branch-misses,L1-dcache-load-misses,LLC-load-misses")
    parser.add_argument("--frequency", type=int, default=99, help="sampling frequency for perf record")
    parser.add_argument("--mode", choices=("stat", "record", "proc"), default="stat")
    parser.add_argument("--flamegraph", type=Path, help="directory containing stackcollapse-perf.pl and flamegraph.pl")
    args = parser.parse_args()
    proc = Path(f"/proc/{args.pid}")
    if sys.platform != "linux" or not proc.is_dir() or not 1 <= args.seconds <= 3600:
        parser.error("requires a live Linux PID and a bounded sample duration")
    if not 1 <= args.frequency <= 4000:
        parser.error("perf sample frequency must be 1 to 4000 Hz")
    args.output.mkdir(parents=True)
    perf_access = Path("/proc/sys/kernel/perf_event_paranoid")
    event_sources = Path("/sys/bus/event_source/devices")
    env = {
        "uname": platform.uname()._asdict(),
        "pid": args.pid,
        "affinity": sorted(os.sched_getaffinity(args.pid)),
        "clock": vars(time.get_clock_info("monotonic")),
        "profiler_mode": args.mode, "events": args.events if args.mode == "stat" else None,
        "frequency": args.frequency if args.mode == "record" else None,
        "perf_event_paranoid": perf_access.read_text().strip() if perf_access.is_file() else None,
        "event_sources": sorted(item.name for item in event_sources.iterdir()) if event_sources.is_dir() else None,
    }
    (args.output / "environment.json").write_text(json.dumps(env, indent=2) + "\n")
    prefix = ["sudo", "-n", args.perf] if args.sudo else [args.perf]
    snapshot(args.pid, args.output / "before")
    status = {"mode": args.mode, "recorded": False}
    if args.mode == "proc":
        time.sleep(args.seconds)
    elif args.mode == "stat":
        results = args.output / "perf-stat.csv"
        status["perf_exit"] = run(
            prefix + ["stat", "--no-big-num", "-x,", "-e", args.events, "-p", str(args.pid),
                      "--", "sleep", str(args.seconds)], results,
        )
        values = {}
        for line in results.read_text().splitlines():
            fields = line.split(",")
            if len(fields) < 3 or not fields[2]:
                continue
            try:
                number = float(fields[0])
            except ValueError:
                if fields[0] in {"<not supported>", "<not counted>"}:
                    values[fields[2]] = {"availability": fields[0]}
            else:
                values[fields[2]] = {"value": number, "unit": fields[1] or None}
        status["counters"] = {event: values.get(event, {"availability": "missing from perf output"})
                              for event in args.events.split(",")}
        if all("value" in values.get(event, {}) for event in ("cycles", "instructions")):
            cycles = values["cycles"]["value"]
            status["ipc"] = values["instructions"]["value"] / cycles if cycles > 0 else None
    else:
        data = args.output / "perf.data"
        status["perf_exit"] = run(
            prefix + ["record", "-e", "cpu-clock:u", "-F", str(args.frequency), "--call-graph", "dwarf,8192",
                      "-p", str(args.pid), "-o", str(data), "--", "sleep", str(args.seconds)],
            args.output / "perf-record.log",
        )
        if status["perf_exit"] == 0:
            stacks = args.output / "stacks.txt"
            status["script_exit"] = run(prefix + [
                "--no-pager", "script", "--no-inline", "-F", "comm,pid,tid,time,event,ip,sym,dso",
                "-i", str(data),
            ], stacks)
            status["recorded"] = status["script_exit"] == 0 and stacks.stat().st_size > 0
            if args.flamegraph and status["recorded"]:
                folded = args.output / "stacks.folded"
                with stacks.open("rb") as source, folded.open("wb") as target:
                    subprocess.run(["perl", str(args.flamegraph / "stackcollapse-perf.pl")],
                                   stdin=source, stdout=target, check=True)
                with folded.open("rb") as source, (args.output / "flamegraph.svg").open("wb") as target:
                    subprocess.run(["perl", str(args.flamegraph / "flamegraph.pl")],
                                   stdin=source, stdout=target, check=True)
    snapshot(args.pid, args.output / "after")
    (args.output / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    if args.mode == "proc":
        return 0
    return int(status.get("perf_exit") != 0 or (args.mode == "record" and not status["recorded"]))


if __name__ == "__main__":
    sys.exit(main())
