#!/usr/bin/env python3
"""Run the cleartext echo capacity fixture with separate Linux CPU affinities."""

import argparse
import os
from pathlib import Path
import subprocess
import sys

from capacity import digest, write_json
from telemetry import H2_LAB, LAB, available_port, http, native_spec, process, proxy_config, until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy", type=Path)
    parser.add_argument("generator", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--protocol", choices=("http1", "http2"), required=True)
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--proxy-cpus", type=int, nargs="+", default=[0])
    parser.add_argument("--backend-cpu", type=int, default=1)
    parser.add_argument("--client-cpu", type=int, nargs="+", default=[2])
    parser.add_argument("--sampler-cpu", type=int)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--queue", type=int, default=64)
    parser.add_argument("--refine-steps", type=int, default=0)
    parser.add_argument("--stability-seconds", type=int, default=0)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=8)
    parser.add_argument("--confirm-seconds", type=int, required=True)
    parser.add_argument("--rates", type=int, nargs="+", required=True)
    parser.add_argument("--p99-ms", type=float, required=True)
    parser.add_argument("--p999-ms", type=float, required=True)
    args = parser.parse_args()

    cpus = [*args.proxy_cpus, args.backend_cpu, *args.client_cpu]
    if args.sampler_cpu is not None:
        cpus.append(args.sampler_cpu)
    if (sys.platform != "linux" or len(set(cpus)) != len(cpus)
            or not set(cpus).issubset(os.sched_getaffinity(0))
            or not 1 <= args.workers <= len(args.proxy_cpus)):
        parser.error("use available, disjoint Linux CPUs and at most one Tokio worker per proxy CPU")

    args.routes = 1
    args.rate = args.rates[0]
    args.proxy = args.proxy.resolve()
    args.generator = args.generator.resolve()
    root = args.output.resolve()
    root.mkdir(parents=True)
    listen, admin, backend_port, stats_port = (available_port() for _ in range(4))
    fixture = LAB / "backend" / "server.mjs" if args.protocol == "http1" else H2_LAB / "backend" / "node.mjs"
    config = root / "proxy.toml"
    config.write_text(proxy_config(args, listen, admin, backend_port, root / "access.jsonl", args.logs))
    write_json(root / "fixture.json", {
        "proxy_sha256": digest(args.proxy), "backend_source_sha256": digest(fixture),
        "proxy_cpus": args.proxy_cpus, "backend_cpu": args.backend_cpu, "client_cpu": args.client_cpu,
        "workers": args.workers, "logs": args.logs, "protocol": args.protocol,
        "sampler_cpu": args.sampler_cpu, "concurrency": args.concurrency, "queue": args.queue,
    })
    target, control = root / "target.json", root / "control.json"
    for path, port in [(target, listen), (control, backend_port)]:
        write_json(path, {**native_spec(args, port), "concurrency": args.concurrency, "queue": args.queue})
    backend_env = {**os.environ, "PORT": str(backend_port), "TLS": "0", "BENCH_H2_TLS": "0",
                   "BENCH_BIND": "127.0.0.1", "BENCH_STATS_PORT": str(stats_port)}
    proxy_env = {**os.environ, "TOKIO_WORKER_THREADS": str(args.workers)}
    with process(["taskset", "-c", str(args.backend_cpu), "node", str(fixture)],
                 root / "backend.log", backend_env) as backend:
        until(backend, lambda: http(stats_port, "/stats"), 5)
        proxy_cpus = ",".join(map(str, args.proxy_cpus))
        with process(["taskset", "-c", proxy_cpus, str(args.proxy), str(config)],
                     root / "proxy.log", proxy_env) as proxy:
            until(proxy, lambda: http(admin, "/ready"), 5)
            sampler = ["--sample-pid", str(proxy.pid)] if args.sampler_cpu is not None else []
            return subprocess.run([
                "taskset", "-c", str(args.sampler_cpu if args.sampler_cpu is not None else args.client_cpu[0]),
                sys.executable,
                str(Path(__file__).with_name("capacity.py")), str(args.generator),
                str(target), str(control), str(root / "measurements"),
                "--confirm-seconds", str(args.confirm_seconds), "--p99-ms", str(args.p99_ms),
                "--p999-ms", str(args.p999_ms), "--rates", *map(str, args.rates),
                "--client-cpus", ",".join(map(str, args.client_cpu)),
                "--refine-steps", str(args.refine_steps), "--stability-seconds", str(args.stability_seconds),
                *sampler,
            ], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
