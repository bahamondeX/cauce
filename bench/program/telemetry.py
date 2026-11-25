#!/usr/bin/env python3
"""Compare two release Cauce binaries with the same open-loop HTTP/1.1 fixture."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import random
import secrets
import socket
import statistics
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from capacity import JITTER_P99_NS, audit, digest, run_cell

LAB = Path(__file__).resolve().parents[1] / "increment7"
H2_LAB = LAB.with_name("increment8")
PREFIX = "bench_cauce_"


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process(command, log, env=None):
    with log.open("w") as output:
        child = subprocess.Popen(command, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def until(child, condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Process {child.pid} exited with code {child.returncode}")
        try:
            result = condition()
            if result:
                return result
        except OSError:
            pass
        time.sleep(0.01)
    raise TimeoutError(f"Timed out waiting for process {child.pid}")


def http(port, path, token=None):
    extra = {"Authorization": "Bearer " + token} if token else {}
    with urlopen(Request(f"http://127.0.0.1:{port}{path}", headers=extra), timeout=1) as response:
        return response.read(), response.headers


def metrics(port, token):
    body, _ = http(port, "/metrics", token)
    return {
        key.removeprefix(PREFIX): int(val)
        for key, val in (row.split() for row in body.decode().splitlines() if row.startswith(PREFIX))
    }


def process_stats(pid):
    if platform.system() == "Linux":
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        counters = {
            key.rstrip(":"): int(value.split()[0])
            for name in ("status", "smaps_rollup")
            for line in (proc / name).read_text().splitlines()
            for key, _, value in [line.partition(":")]
            if key in {"VmRSS", "Pss", "voluntary_ctxt_switches", "nonvoluntary_ctxt_switches"}
        }
        return {"cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                "rss_kib": counters.pop("VmRSS"), **counters}
    cpu, rss = subprocess.check_output(["ps", "-p", str(pid), "-o", "time=,rss="],
                                       text=True).strip().split()
    seconds = 0.0
    for part in cpu.split(":"):
        seconds = seconds * 60 + float(part)
    return {"cpu_seconds": seconds, "rss_kib": int(rss)}


def native_spec(args, port):
    return {
        "url": f"http://127.0.0.1:{port}/echo",
        "protocol": args.protocol,
        "rate": args.rate,
        "seconds": args.seconds,
        "warmup_seconds": args.warmup,
        "concurrency": 32,
        "queue": 64,
        "request_bytes": 0,
        "response_bytes": 1024,
        "response_byte": ord("x"),
    }


def proxy_config(args, listen, admin, backend_port, path, logs_on):
    source = (LAB / "configs" / "cauce-clear.toml" if args.protocol == "http1"
              else H2_LAB / "configs" / "cauce-h2-clear.toml")
    config = source.read_text().replace('name="bench8-h2-clear"', 'name="bench-cauce"')
    for old, new in {
        "0.0.0.0:8080": f"127.0.0.1:{listen}",
        "127.0.0.1:9090": f"127.0.0.1:{admin}",
        "proxy:8080": f"127.0.0.1:{listen}",
        'address="backend:8000"': f'address="127.0.0.1:{backend_port}"',
        'address="backend-h2:8000"': f'address="127.0.0.1:{backend_port}"',
        '"/var/log/cauce/access.jsonl"': json.dumps(str(path)),
        "enabled=false": f"enabled={str(logs_on).lower()}",
        "rotate_bytes=16777216": "rotate_bytes=268435456",
    }.items():
        config = config.replace(old, new)
    for index in range(1, args.routes):
        config += (f'\n[[routes]]\nid="unused-{index}"\nhost="127.0.0.1:{listen}"\n'
                   f'path_prefix="/unused-{index}"\nbalance="round_robin"\nupstreams=["backend"]\n')
    return config


def native_summary(path, spec):
    raw = path / "offers.jsonl"
    summary = json.loads((path / "offers.summary.json").read_text())
    measured = audit(raw, summary, spec)
    warmup_errors = summary["warmup_offers"] - summary["warmup_successes"]
    rows = [json.loads(row) for row in raw.read_text().splitlines()]
    return {
        "offered": measured["offered"],
        "offer_window_goodput_per_second": measured["goodput_per_s"],
        "warmup_errors": warmup_errors,
        "warmup_rejections": 0,
        "percentiles_ms": {"scheduler_lag": {"all": {"p99": measured["scheduler_p99_ns"] / 1e6}}},
        "invalid_reasons": measured["invalid_reasons"],
    }, [(row["end_ns"] - row["scheduled_ns"]) / 1e6 for row in rows if row["outcome"] == "success"]


def compare_cell(args, path, binary, logs_on, backend_port, stats_port, cpus, workers):
    path.mkdir()
    listen, admin = available_port(), available_port()
    proxy_log = path / "access.jsonl"
    config_path = path / "proxy.toml"
    config_path.write_text(proxy_config(args, listen, admin, backend_port, proxy_log, logs_on))
    token = secrets.token_hex(32)
    env = {**os.environ, "TOKIO_WORKER_THREADS": str(workers), "CAUCE_ADMIN_TOKEN": token}
    warm, resume, done, release = [path / n for n in ("warm", "resume", "done", "release")]
    command = cpus[0] + [str(binary), str(config_path)]
    client_command = cpus[2] + [
        sys.executable, str(LAB / "load.py"), "--url", f"http://127.0.0.1:{listen}",
        "--scenario", "normal", "--expect-bytes", "1024", "--concurrency", "32",
        "--rate", str(args.rate), "--seconds", str(args.seconds), "--warmup", str(args.warmup),
        "--output", str(path / "offers.jsonl"), "--warmup-done-file", str(warm),
        "--resume-file", str(resume), "--measurement-done-file", str(done),
        "--measurement-release-file", str(release), "--seed", str(args.seed),
    ]
    spec = None
    if args.native_load:
        spec = native_spec(args, listen) | {
            "warmup_done_file": str(warm), "resume_file": str(resume),
            "measurement_done_file": str(done), "measurement_release_file": str(release),
        }
        save(path / "load.json", spec)
        client_command = cpus[2] + [
            str(args.native_load.resolve()), str(path / "load.json"), str(path / "offers.jsonl"),
            str(path / "offers.summary.json"),
        ]
    with process(command, path / "proxy.log", env) as proxy:
        until(proxy, lambda: http(admin, "/ready"), 5)
        body, headers = http(listen, "/echo")
        if body != b"x" * 1024 or not headers.get("x-request-id"):
            raise RuntimeError("Fixture response or request identity failed")
        with process(client_command, path / "client.log") as client:
            until(client, warm.exists, args.warmup + 10)
            http(stats_port, "/reset")
            backend_start = json.loads(http(stats_port, "/stats")[0])
            metric_start = metrics(admin, token)
            stat_start = process_stats(proxy.pid)
            resume.touch()
            until(client, done.exists, args.seconds + 15)
            stat_end = process_stats(proxy.pid)
            metric_end = metrics(admin, token)
            backend_end = json.loads(http(stats_port, "/stats")[0])
            release.touch()
            if client.wait(timeout=20):
                raise RuntimeError("Load generator failed, see client.log")
        proxy.terminate()
        if proxy.wait(timeout=8):
            raise RuntimeError("Proxy exited unsuccessfully")
    save(path / "counters.json", {
        "proxy_before": metric_start, "proxy_after": metric_end,
        "backend_before": backend_start, "backend_after": backend_end,
        "process_before": stat_start, "process_after": stat_end,
    })
    if spec is not None:
        load, latency = native_summary(path, spec)
    else:
        load = json.loads((path / "offers.summary.json").read_text())
        latency = []
        with (path / "offers.jsonl").open() as file:
            for line in file:
                row = json.loads(line)
                if row["outcome"] == "success":
                    latency.append(row["scheduled_latency_ms"])
    successful = len(latency)
    delta = {name: metric_end[name] - value for name, value in metric_start.items()}
    log_records = 0
    if proxy_log.exists():
        with proxy_log.open() as file:
            for line in file:
                event = json.loads(line)
                if (event["outcome"] != "complete" or len(event["attempts"]) != 1
                        or event["response_bytes"] != 1024):
                    raise RuntimeError("Incomplete access-log record, see access.jsonl")
                log_records += 1
    failures = list(load.get("invalid_reasons", []))
    if successful != load["offered"] or load["offered"] != math.ceil(args.rate * args.seconds):
        failures.append("Failed/lost load offers or incorrect offered count")
    if any(load[k] for k in ("warmup_errors", "warmup_rejections")):
        failures.append("Warmup errors or rejections")
    lag = load["percentiles_ms"]["scheduler_lag"]["all"]["p99"]
    if lag is None or lag > args.max_lag_p99:
        failures.append("Generator p99 lag exceeded its budget")
    if (delta["requests_total"] != successful or delta["responses_total"] != successful
            or delta["response_bytes_total"] != successful * 1024
            or backend_end["requests"] - backend_start["requests"] != successful
            or backend_end["peak_connections"] > 32):
        failures.append("Client, proxy, and backend accounting or capacity differs")
    if (metric_end["log_dropped_total"] or metric_end["log_io_errors_total"]
            or log_records != (metric_end["requests_total"] if logs_on else 0)):
        failures.append("Access-log losses or unexpected records")
    latency.sort()
    return {
        "failures": failures, "offers": load["offered"], "successful": successful,
        "goodput_per_second": load["offer_window_goodput_per_second"],
        "scheduler_p99_ms": lag,
        "latency_ms": {
            name: latency[math.ceil(len(latency) * q) - 1] if latency else None
            for name, q in (("p50", .5), ("p95", .95), ("p99", .99), ("p99.9", .999))
        },
        "cpu_us_per_success": 1e6 * (stat_end["cpu_seconds"] - stat_start["cpu_seconds"]) / max(1, successful),
        "rss_kib_at_end": stat_end["rss_kib"], "logged": log_records,
        "upstream_reused": delta["upstream_reused_total"],
    }


def summarize(cells, seed):
    rng = random.Random(seed)
    summary = {}
    fields = {
        "cpu_us_per_success": lambda cell: cell["cpu_us_per_success"],
        "goodput_per_second": lambda cell: cell["goodput_per_second"],
        "rss_kib_at_end": lambda cell: cell["rss_kib_at_end"],
    }
    for percentile in ("p50", "p95", "p99", "p99.9"):
        fields[f"{percentile}_ms"] = lambda cell, name=percentile: cell["latency_ms"][name]
    for logs in ("off", "on"):
        valid = [c for c in cells if c["logs"] == logs and not c["failures"]]
        before = {c["repeat"]: c for c in valid if c["binary"] == "before"}
        after = {c["repeat"]: c for c in valid if c["binary"] == "after"}
        pairs = sorted(before.keys() & after.keys())
        if not pairs:
            continue
        metrics = {}
        for key, value in fields.items():
            baseline = [value(before[i]) for i in pairs]
            candidate = [value(after[i]) for i in pairs]
            deltas = [b - a for a, b in zip(baseline, candidate)]
            interval = None
            if len(pairs) >= 7:
                bootstrap = sorted(statistics.median(rng.choices(deltas, k=len(deltas))) for _ in range(10_000))
                interval = [bootstrap[250], bootstrap[9749]]
            metrics[key] = {
                "before": statistics.median(baseline),
                "after": statistics.median(candidate),
                "paired_median_delta": statistics.median(deltas),
                "paired_delta_ci95": interval,
            }
        cpu, p99 = metrics["cpu_us_per_success"], metrics["p99_ms"]
        summary[logs] = {
            "pairs": len(pairs),
            "before_cpu_us": cpu["before"],
            "after_cpu_us": cpu["after"],
            "paired_cpu_delta_us": cpu["paired_median_delta"],
            "paired_cpu_delta_ci95_us": cpu["paired_delta_ci95"],
            "before_p99_ms": p99["before"],
            "after_p99_ms": p99["after"],
            "metrics": metrics,
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("before", "after", "output"):
        parser.add_argument(arg, type=Path)
    parser.add_argument("--rate", type=int, default=1000)
    parser.add_argument("--seconds", type=int, default=6)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--max-lag-p99", type=float, default=10)
    parser.add_argument("--native-load", type=Path)
    parser.add_argument("--protocol", choices=("http1", "http2"), default="http1")
    parser.add_argument("--routes", type=int, choices=(1, 16, 128, 1024), default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--candidate-workers", type=int)
    parser.add_argument("--cpu-sets", nargs=3, metavar=("PROXY_SET", "BACKEND_SET", "CLIENT_SET"))
    parser.add_argument("--seed", type=int, default=32968)
    parser.add_argument("--cpus", type=int, nargs=3, metavar=("PROXY", "BACKEND", "CLIENT"))
    args = parser.parse_args()
    if args.protocol == "http2" and not args.native_load:
        parser.error("the HTTP/2 workload needs the native client")
    if min(args.rate, args.seconds, args.warmup, args.repetitions, args.max_lag_p99) <= 0:
        parser.error("Load, timing, and repetition settings must be positive")
    if (not args.max_lag_p99 <= JITTER_P99_NS / 1e6 or args.workers < 1
            or (args.candidate_workers is not None and args.candidate_workers < 1)):
        parser.error("Scheduler p99 limit cannot exceed 10 ms and workers must be positive")
    if args.cpus and (platform.system() != "Linux" or len(set(args.cpus)) != 3):
        parser.error("--cpus requires Linux and three distinct CPU indices")
    if args.cpu_sets:
        try:
            sets = [set(map(int, cpus.split(","))) for cpus in args.cpu_sets]
        except ValueError:
            parser.error("--cpu-sets needs comma-delimited Linux CPU indices")
        if (args.cpus or platform.system() != "Linux"
                or sum(map(len, sets)) != len(set.union(*sets))
                or not set.union(*sets).issubset(os.sched_getaffinity(0))):
            parser.error("--cpu-sets requires three non-overlapping available Linux CPU sets")
    affinity = args.cpu_sets or args.cpus
    cpus = [["taskset", "-c", str(cpu)] for cpu in affinity] if affinity else [[], [], []]
    binaries = {name: getattr(args, name).resolve() for name in ("before", "after")}
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    order = [(name, logs) for name in binaries for logs in ("off", "on")]
    rng = random.Random(args.seed)
    hashes = {name: digest(path) for name, path in binaries.items()}
    environment = {
        "platform": platform.platform(), "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        "python": sys.version, "started_utc": datetime.now(timezone.utc).isoformat(),
        "binary_sha256": hashes,
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "cpu_precision": "Linux /proc clock ticks" if platform.system() == "Linux" else "ps cumulative CPU time",
    }
    if args.native_load:
        environment["native_load_sha256"] = digest(args.native_load)
    save(args.output / "environment.json", environment)
    cells = []
    backend_port, stats_port = available_port(), available_port()
    env = {**os.environ, "PORT": str(backend_port), "TLS": "0",
           "BENCH_STATS_PORT": str(stats_port), "BENCH_BIND": "127.0.0.1", "BENCH_H2_TLS": "0"}
    fixture = LAB / "backend" / "server.mjs" if args.protocol == "http1" else H2_LAB / "backend" / "node.mjs"
    with process(cpus[1] + ["node", str(fixture)],
                 args.output / "backend.log", env) as backend:
        until(backend, lambda: http(stats_port, "/stats"), 5)
        control_spec = None
        if args.native_load:
            spec = native_spec(args, backend_port)
            spec["rate"] *= 2
            # Inherit the same client affinity during qualification and measurement.
            control = run_cell(args.native_load.resolve(), args.output, spec, "control-before", cpus[2])
            if (control["invalid_reasons"] or control.get("successful") != control.get("offered")
                    or control.get("warmup_successes") != spec["rate"] * args.warmup):
                save(args.output / "qualification.json", {"status": "REJECTED_PREFLIGHT"})
                return 2
            control_spec = spec
        for repeat in range(1, args.repetitions + 1):
            rng.shuffle(order)
            for name, logs in order:
                until(backend, lambda: json.loads(http(stats_port, "/stats")[0])["connections"] == 0, 5)
                workers = (args.candidate_workers or args.workers) if name == "after" else args.workers
                cell = compare_cell(args, args.output / f"{repeat}-{name}-{logs}",
                                    binaries[name], logs == "on", backend_port, stats_port, cpus, workers)
                cells.append({"repeat": repeat, "binary": name, "logs": logs, "workers": workers, **cell})
                save(args.output / "cells.json", cells)
                save(args.output / "summary.json", summarize(cells, args.seed))
                print(json.dumps(cells[-1]), flush=True)
        if control_spec:
            control = run_cell(args.native_load.resolve(), args.output, control_spec, "control-after", cpus[2])
            if (control["invalid_reasons"] or control.get("successful") != control.get("offered")
                    or control.get("warmup_successes") != control_spec["rate"] * args.warmup):
                save(args.output / "qualification.json", {"status": "REJECTED_POSTFLIGHT"})
                return 2
        save(args.output / "qualification.json", {
            "status": "COMPLETE", "measured_cells": len(cells),
            "rejected_cells": sum(bool(cell["failures"]) for cell in cells),
        })
    return int(any(c["failures"] for c in cells))


if __name__ == "__main__":
    sys.exit(main())
