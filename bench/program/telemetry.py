#!/usr/bin/env python3
"""Compare two release Cauce binaries with the same open-loop HTTP/1.1 fixture."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
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

LAB = Path(__file__).resolve().parents[1] / "increment7"
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


def compare_cell(args, path, binary, logs_on, backend_port, stats_port, cpus):
    path.mkdir()
    listen, admin = available_port(), available_port()
    proxy_log = path / "access.jsonl"
    config = (LAB / "configs" / "cauce-clear.toml").read_text()
    for old, new in {
        "0.0.0.0:8080": f"127.0.0.1:{listen}",
        "127.0.0.1:9090": f"127.0.0.1:{admin}",
        "proxy:8080": f"127.0.0.1:{listen}",
        "backend:8000": f"127.0.0.1:{backend_port}",
        '"/var/log/cauce/access.jsonl"': json.dumps(str(proxy_log)),
        "enabled=false": f"enabled={str(logs_on).lower()}",
        "rotate_bytes=16777216": "rotate_bytes=268435456",
    }.items():
        config = config.replace(old, new)
    config_path = path / "proxy.toml"
    config_path.write_text(config)
    token = secrets.token_hex(32)
    env = {**os.environ, "TOKIO_WORKER_THREADS": "1", "CAUCE_ADMIN_TOKEN": token}
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
    failures = []
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
    for logs in ("off", "on"):
        valid = [c for c in cells if c["logs"] == logs and not c["failures"]]
        before = {c["repeat"]: c for c in valid if c["binary"] == "before"}
        after = {c["repeat"]: c for c in valid if c["binary"] == "after"}
        pairs = sorted(before.keys() & after.keys())
        if not pairs:
            continue
        left = [before[i]["cpu_us_per_success"] for i in pairs]
        right = [after[i]["cpu_us_per_success"] for i in pairs]
        diff = [b - a for a, b in zip(left, right)]
        bootstrap = sorted(statistics.median(rng.choices(diff, k=len(diff))) for _ in range(10_000))
        summary[logs] = {
            "pairs": len(pairs), "before_cpu_us": statistics.median(left),
            "after_cpu_us": statistics.median(right),
            "paired_cpu_delta_us": statistics.median(diff),
            "paired_cpu_delta_ci95_us": [bootstrap[250], bootstrap[9749]] if len(pairs) >= 3 else None,
            "before_p99_ms": statistics.median(before[i]["latency_ms"]["p99"] for i in pairs),
            "after_p99_ms": statistics.median(after[i]["latency_ms"]["p99"] for i in pairs),
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
    parser.add_argument("--seed", type=int, default=32968)
    parser.add_argument("--cpus", type=int, nargs=3, metavar=("PROXY", "BACKEND", "CLIENT"))
    args = parser.parse_args()
    if min(args.rate, args.seconds, args.warmup, args.repetitions, args.max_lag_p99) <= 0:
        parser.error("Load, timing, and repetition settings must be positive")
    if args.cpus and (platform.system() != "Linux" or len(set(args.cpus)) != 3):
        parser.error("--cpus requires Linux and three distinct CPU indices")
    cpus = [["taskset", "-c", str(cpu)] for cpu in args.cpus] if args.cpus else [[], [], []]
    binaries = {name: getattr(args, name).resolve() for name in ("before", "after")}
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    order = [(name, logs) for name in binaries for logs in ("off", "on")]
    rng = random.Random(args.seed)
    hashes = {}
    for name, path in binaries.items():
        digest = hashlib.sha256()
        with path.open("rb") as binary:
            for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[name] = digest.hexdigest()
    environment = {
        "platform": platform.platform(), "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        "python": sys.version, "started_utc": datetime.now(timezone.utc).isoformat(),
        "binary_sha256": hashes,
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "cpu_precision": "Linux /proc clock ticks" if platform.system() == "Linux" else "ps cumulative CPU time",
    }
    save(args.output / "environment.json", environment)
    cells = []
    backend_port, stats_port = available_port(), available_port()
    env = {**os.environ, "PORT": str(backend_port), "TLS": "0",
           "BENCH_STATS_PORT": str(stats_port), "BENCH_BIND": "127.0.0.1"}
    with process(cpus[1] + ["node", str(LAB / "backend" / "server.mjs")],
                 args.output / "backend.log", env) as backend:
        until(backend, lambda: http(stats_port, "/stats"), 5)
        for repeat in range(1, args.repetitions + 1):
            rng.shuffle(order)
            for name, logs in order:
                until(backend, lambda: json.loads(http(stats_port, "/stats")[0])["connections"] == 0, 5)
                cell = compare_cell(args, args.output / f"{repeat}-{name}-{logs}",
                                    binaries[name], logs == "on", backend_port, stats_port, cpus)
                cells.append({"repeat": repeat, "binary": name, "logs": logs, **cell})
                save(args.output / "cells.json", cells)
                save(args.output / "summary.json", summarize(cells, args.seed))
                print(json.dumps(cells[-1]), flush=True)
    return int(any(c["failures"] for c in cells))


if __name__ == "__main__":
    sys.exit(main())
