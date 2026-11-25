#!/usr/bin/env python3
"""Profile an isolated native Cauce process under the shared HTTP fixture."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from capacity import audit, digest, write_json
from telemetry import LAB, H2_LAB, available_port, http, native_spec, process, proxy_config, until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy", type=Path)
    parser.add_argument("generator", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--protocol", choices=("http1", "http2"), required=True)
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--perf", default="perf")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--mode", choices=("stat", "record", "proc"), default="record")
    parser.add_argument("--flamegraph", type=Path)
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--rate", type=int, default=500)
    parser.add_argument("--cpus", type=int, nargs=3, default=[0, 1, 2])
    args = parser.parse_args()
    if (sys.platform != "linux" or len(set(args.cpus)) != 3
            or not set(args.cpus).issubset(os.sched_getaffinity(0))
            or args.seconds < 3 or args.rate < 1):
        parser.error("requires Linux, three available CPU indices and positive workload settings")
    args.routes = 1
    args.warmup = 2
    root = args.output.resolve()
    root.mkdir(parents=True)
    args.proxy = args.proxy.resolve()
    args.generator = args.generator.resolve()
    listen, admin, backend_port, stats_port = (available_port() for _ in range(4))
    fixture = LAB / "backend" / "server.mjs" if args.protocol == "http1" else H2_LAB / "backend" / "node.mjs"
    env = {**os.environ, "PORT": str(backend_port), "TLS": "0", "BENCH_H2_TLS": "0",
           "BENCH_BIND": "127.0.0.1", "BENCH_STATS_PORT": str(stats_port)}
    config = root / "proxy.toml"
    config.write_text(proxy_config(args, listen, admin, backend_port, root / "access.jsonl", args.logs))
    warm, resume = root / "warm", root / "resume"
    spec = native_spec(args, listen) | {
        "warmup_done_file": str(warm), "resume_file": str(resume), "seconds": args.seconds + 3,
    }
    write_json(root / "load.json", spec)
    write_json(root / "identities.json", {
        "proxy_sha256": digest(args.proxy), "generator_sha256": digest(args.generator),
        "proxy_cpu": args.cpus[0], "backend_cpu": args.cpus[1], "client_cpu": args.cpus[2],
    })
    proxy_cpu, backend_cpu, client_cpu = (["taskset", "-c", str(cpu)] for cpu in args.cpus)
    with process(backend_cpu + ["node", str(fixture)], root / "backend.log", env) as backend:
        until(backend, lambda: http(stats_port, "/stats"), 5)
        with process(proxy_cpu + [str(args.proxy), str(config)], root / "proxy.log",
                     {**os.environ, "TOKIO_WORKER_THREADS": "1"}) as proxy:
            until(proxy, lambda: http(admin, "/ready"), 5)
            raw, summary = root / "offers.jsonl", root / "offers.summary.json"
            command = client_cpu + [str(args.generator), str(root / "load.json"), str(raw), str(summary)]
            with process(command, root / "client.log") as client:
                until(client, warm.exists, 10)
                profiler = [
                    sys.executable, str(Path(__file__).with_name("profile-linux.py")), str(proxy.pid),
                    str(root / "profile"), "--mode", args.mode, "--seconds", str(args.seconds),
                    "--perf", args.perf,
                ]
                if args.sudo:
                    profiler.append("--sudo")
                if args.flamegraph:
                    profiler.extend(["--flamegraph", str(args.flamegraph)])
                resume.touch()
                time.sleep(0.5)
                capture = subprocess.run(profiler, check=False, timeout=args.seconds + 35)
                if client.wait(timeout=args.seconds + 10):
                    raise RuntimeError("native load failed")
            results = audit(raw, json.loads(summary.read_text()), spec)
            results["profile_exit"] = capture.returncode
            results["usage"] = "diagnostic samples under profiler overhead"
            write_json(root / "audit.json", results)
            if capture.returncode or results["successful"] != results["offered"]:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
