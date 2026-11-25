#!/usr/bin/env python3
"""Exercise both pool strategies against real HTTP/1.1 and HTTP/2 backends."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

from capacity import digest, quantile, run_cell, write_json
from telemetry import H2_LAB, LAB, available_port, endpoint_config, http, native_spec, process, proxy_config, until


def exercise(args, dest, protocol, strategy, endpoints):
    args.protocol = protocol
    dest.mkdir()
    listen, admin, backend_port, stats_port = (available_port() for _ in range(4))
    config = dest / "proxy.toml"
    access = dest / "access.jsonl"
    config.write_text(endpoint_config(
        proxy_config(args, listen, admin, backend_port, access, True), endpoints, args.skewed))
    backend_env = {**os.environ, "PORT": str(backend_port), "TLS": "0", "BENCH_H2_TLS": "0",
                   "BENCH_BIND": "127.0.0.1", "BENCH_STATS_PORT": str(stats_port)}
    proxy_env = {**os.environ, "TOKIO_WORKER_THREADS": str(args.workers),
                 "CAUCE_POOL_STRATEGY": strategy,
                 "CAUCE_TRANSPORT_DIAGNOSTICS": str(int(args.diagnostics))}
    fixture = (LAB / "backend/server.mjs" if protocol == "http1" else H2_LAB / "backend/node.mjs")
    audits = []
    with process(["node", str(fixture)], dest / "backend.log", backend_env) as backend:
        until(backend, lambda: http(stats_port, "/stats"), 5)
        with process([str(args.proxy), str(config)], dest / "proxy.log", proxy_env) as proxy:
            until(proxy, lambda: http(admin, "/ready"), 5)
            spec = native_spec(args, listen)
            spec["seconds"] = max(1, -(-endpoints // spec["rate"]))
            audits.append(run_cell(args.generator, dest, spec, "uniform"))
            if args.skewed:
                spec.update(url=spec["url"] + "-hot", seconds=2)
                audits.append(run_cell(args.generator, dest, spec, "hot"))
        backend_stats = json.loads(http(stats_port, "/stats")[0])
        write_json(dest / "backend-stats.json", backend_stats)
    expected = sum(audit.get("offered", 0) for audit in audits)
    events = [json.loads(row) for row in access.read_text().splitlines()]
    by_endpoint = Counter(e["upstream_id"] for e in events)
    waits, holds = [], []
    trace = None
    if args.diagnostics:
        for line in (dest / "proxy.log").open():
            event = json.loads(line)
            if event["event"] == "pool_lock":
                waits.append(event["fields"]["wait_ns"])
                holds.append(event["fields"]["hold_ns"])
            elif event["event"] == "transport_diagnostics_end":
                trace = event
    accepted = (all(audit.get("successful") == audit.get("offered") for audit in audits)
                and len(events) == expected and len({e["event_id"] for e in events}) == expected
                and len(by_endpoint) == endpoints and all(e["outcome"] == "complete" for e in events))
    return {"protocol": protocol, "strategy": strategy, "endpoints": endpoints,
            "workers": args.workers, "skewed": args.skewed, "successes": sum(a.get("successful",0) for a in audits),
            "offered": expected, "correctness_passed": accepted, "unique_endpoints": len(by_endpoint),
            "peak_backend_connections": backend_stats["peak_connections"],
            "lock_wait_p99_ns": quantile(waits, .99), "lock_hold_p99_ns": quantile(holds, .99),
            "diagnostic_capture": trace}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy", type=Path)
    parser.add_argument("generator", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--protocols", nargs="+", choices=("http1", "http2"), default=["http1", "http2"])
    parser.add_argument("--strategies", nargs="+", choices=("compact", "sharded"), default=["compact", "sharded"])
    parser.add_argument("--endpoints", type=int, nargs="+", default=[1, 4, 16, 64, 256, 1024])
    parser.add_argument("--rate", type=int, default=128)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--skewed", action="store_true", help="add a phase directed entirely at the hot endpoint")
    parser.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args()
    if any(not 1 <= count <= 1024 for count in args.endpoints) or not 1 <= args.workers <= 8 or args.rate < 1:
        parser.error("use 1–1024 endpoints, 1–8 workers and a positive rate")
    args.proxy, args.generator, args.output = map(Path.resolve, (args.proxy, args.generator, args.output))
    args.output.mkdir(parents=True)
    args.routes, args.seconds, args.warmup = 1, 1, 0
    report = {"proxy_sha256": digest(args.proxy), "generator_sha256": digest(args.generator), "cells": []}
    for protocol in args.protocols:
        for strategy in args.strategies:
            for count in args.endpoints:
                name = f"{protocol}-{strategy}-{count}"
                result = exercise(args, args.output / name, protocol, strategy, count)
                report["cells"].append(result)
                write_json(args.output / "matrix.json", report)
                print(json.dumps(result), flush=True)
    return int(not all(row["correctness_passed"] for row in report["cells"]))


if __name__ == "__main__":
    sys.exit(main())
