#!/usr/bin/env python3
"""Replay the HTTP/1.1 transport incident and join failures to proxy events."""

import argparse
from collections import defaultdict, Counter
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

from capacity import digest, run_cell, write_json
from telemetry import LAB, available_port, http, native_spec, process, proxy_config, until


def correlate(proxy_log, offers, warmup):
    requests, peer_sockets = {}, defaultdict(set)
    by_request, by_upstream, by_downstream = (defaultdict(list) for _ in range(3))
    counts = Counter()
    capture = None
    with proxy_log.open() as stream:
        for line in stream:
            row = json.loads(line)
            name = row["event"]
            counts[name] += 1
            fields = row.get("fields", {})
            if name == "transport_diagnostics_end":
                capture = row
            if name == "request_started" and fields["offer"] is not None:
                requests[fields["offer"]] = fields
            if name == "downstream_accept" and "peer_port" in fields:
                peer_sockets[fields["peer_port"]].add(fields["connection"])
            if "request_id" in fields:
                by_request[fields["request_id"]].append(row)
            if name.startswith("socket_") or name == "transport_task_end":
                side = fields.get("side") or fields["kind"]
                index = by_downstream if side == "downstream" else by_upstream
                index[fields["connection"]].append(row)
            elif name.startswith("upstream_") and "connection" in fields:
                by_upstream[fields["connection"]].append(row)
            elif name.startswith("downstream_") and "connection" in fields:
                by_downstream[fields["connection"]].append(row)

    def join(row, rows, offset):
        req = requests.get(row["n"] + offset, {})
        related = by_request.get(req.get("request_id"), [])
        sockets = peer_sockets.get(row.get("local_port"), set())
        downstream = req.get("downstream")
        if downstream is None and len(sockets) == 1:
            downstream = next(iter(sockets))
        upstream = {event["fields"]["connection"] for event in related
                    if event["event"] == "upstream_lease_acquired"}
        peers = by_downstream.get(downstream, []) + [e for conn in upstream for e in by_upstream[conn]]
        if related:
            first, last = min(e["seq"] for e in related), max(e["seq"] for e in related)
            # Keep a small adjacent I/O window, plus the socket's terminal events.
            peers = [e for e in peers if first - 64 <= e["seq"] <= last + 64
                     or e["event"] in ("downstream_accept", "transport_task_end", "socket_read_error",
                                       "socket_read_eof", "socket_write_error")]
        events = {e["seq"]: e for e in related + peers}
        return {
            "phase": "warmup" if offset else "measurement", "offer": row, "request": req or None,
            "peer_socket_candidates": sorted(sockets), "downstream": downstream,
            "nearest_dispatch": [r["n"] for r in rows if r["start_ns"] is not None
                                 and row["start_ns"] is not None
                                 and abs(r["start_ns"] - row["start_ns"]) <= 1_000_000],
            "events": [events[seq] for seq in sorted(events)],
        }

    failures, tail = [], []
    for path, offset in [(offers, 0), (warmup, 1_000_000_000)]:
        if not path.exists():
            continue
        with path.open() as stream:
            rows = [json.loads(line) for line in stream]
        failures.extend(join(row, rows, offset) for row in rows if row["outcome"] != "success")
        if not offset:
            slowest = sorted((r for r in rows if r["outcome"] == "success"),
                             key=lambda row: row["end_ns"] - row["scheduled_ns"], reverse=True)[:12]
            tail = [join(row, rows, offset) for row in slowest]
    truncated = bool(counts["transport_diagnostics_truncated"] or counts["transport_diagnostics_flush_lost"]
                     or (capture and (capture["dropped"] or capture["attempted"] > capture["limit"])))
    return {"event_counts": counts, "truncated": truncated, "capture": capture,
            "failures": failures, "tail": tail}


def sample_sockets(proxy, ports, output, stop):
    terms = (f"{direction} = :{port}" for port in ports for direction in ("sport", "dport"))
    predicate = f"( {' or '.join(terms)} )"
    with output.open("w") as stream:
        try:
            while not stop.is_set():
                stats = {}
                for thread in Path(f"/proc/{proxy}/task").glob("*"):
                    try:
                        stats[thread.name] = (thread / "schedstat").read_text().strip()
                    except FileNotFoundError:
                        pass
                result = subprocess.run(["ss", "-tinm", predicate], check=False, text=True,
                                        capture_output=True, timeout=2)
                stream.write(json.dumps({"monotonic_ns": time.monotonic_ns(), "schedstat": stats,
                                         "ss": result.stdout, "stderr": result.stderr,
                                         "exit": result.returncode}) + "\n")
                stream.flush()
                stop.wait(.25)
        except (OSError, subprocess.SubprocessError) as error:
            stream.write(json.dumps({"unavailable": str(error)}) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy", type=Path)
    parser.add_argument("generator", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-counts", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--rate", type=int, default=400)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--sample-linux", action="store_true")
    parser.add_argument("--plain", action="store_true", help="replay without proxy diagnostics and offer headers")
    args = parser.parse_args()
    if sys.platform != "linux" or not {0, 1, 2, 3} <= os.sched_getaffinity(0):
        parser.error("this incident replay uses guest CPUs 0 through 3")
    if any(w not in (1, 2, 4) for w in args.worker_counts):
        parser.error("worker counts must be 1, 2 or 4")
    args.protocol, args.warmup, args.routes = "http1", 3, 1
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    client = ["taskset", "-c", "2"]
    fixture = LAB / "backend/server.mjs"
    results = []
    for workers in args.worker_counts:
        dest = args.output / f"workers-{workers}"
        dest.mkdir()
        listen, admin, backend_port, stats_port = (available_port() for _ in range(4))
        env = {**os.environ, "PORT": str(backend_port), "TLS": "0", "BENCH_BIND": "127.0.0.1",
               "BENCH_STATS_PORT": str(stats_port)}
        proxy_env = {**os.environ, "TOKIO_WORKER_THREADS": str(workers),
                     "CAUCE_TRANSPORT_DIAGNOSTICS": str(int(not args.plain))}
        proxy_cpus = {1: "0", 2: "0,3", 4: "0-3"}[workers]
        config = dest / "proxy.toml"
        config.write_text(proxy_config(args, listen, admin, backend_port, dest / "access.jsonl", False))
        with process(["taskset", "-c", "1", "node", str(fixture)], dest / "backend.log", env) as backend:
            until(backend, lambda: http(stats_port, "/stats"), 5)
            control = {**native_spec(args, backend_port), "rate": args.rate * 2, "seconds": 5}
            before = run_cell(args.generator.resolve(), dest, control, "direct-before", client)
            with process(["taskset", "-c", proxy_cpus, str(args.proxy.resolve()), str(config)],
                         dest / "proxy.log", proxy_env) as proxy:
                until(proxy, lambda: http(admin, "/ready"), 5)
                spec = {**native_spec(args, listen), "trace_offers": not args.plain}
                stop = threading.Event()
                sampler = threading.Thread(target=sample_sockets, args=(
                    proxy.pid, [listen, backend_port], dest / "linux-sockets.jsonl", stop))
                if args.sample_linux:
                    sampler.start()
                try:
                    target = run_cell(args.generator.resolve(), dest, spec, "target", client)
                finally:
                    stop.set()
                    if args.sample_linux:
                        sampler.join()
            after = run_cell(args.generator.resolve(), dest, control, "direct-after", client)
        transport = correlate(dest / "proxy.log", dest / "target/offers.jsonl",
                              dest / "target/offers.warmup.jsonl")
        write_json(dest / "correlation.json", transport)
        results.append({"workers": workers, "affinity": proxy_cpus, "diagnostics": not args.plain,
                        "target": target, "direct_before": before, "direct_after": after,
                        "event_counts": transport["event_counts"], "truncated": transport["truncated"],
                        "capture": transport["capture"]})
    write_json(args.output / "replay.json", {"proxy_sha256": digest(args.proxy),
                                            "generator_sha256": digest(args.generator),
                                            "results": results})


if __name__ == "__main__":
    main()
