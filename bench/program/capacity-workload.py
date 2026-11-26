#!/usr/bin/env python3
"""Run the cleartext echo capacity fixture with separate Linux CPU affinities."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from capacity import digest, passes, run_cell, write_json
from telemetry import H2_LAB, LAB, available_port, endpoint_config, http, native_spec, process, proxy_config, until


def nginx_config(listen, backend_port):
    return f"""
worker_processes 1;
master_process off;
daemon off;
pid nginx.pid;
error_log stderr error;
events {{ worker_connections 256; }}
http {{
    access_log off;
    client_body_temp_path client-body;
    proxy_temp_path proxy-temp;
    fastcgi_temp_path fastcgi-temp;
    uwsgi_temp_path uwsgi-temp;
    scgi_temp_path scgi-temp;
    limit_conn_zone $binary_remote_addr zone=client:1m;
    upstream origin {{
        zone origin 64k;
        server 127.0.0.1:{backend_port} max_conns=32 max_fails=0;
        keepalive 32;
        keepalive_timeout 15s;
        keepalive_time 60s;
        keepalive_requests 1000000;
    }}
    server {{
        listen 127.0.0.1:{listen} backlog=128;
        client_header_timeout 5s;
        keepalive_timeout 5s;
        keepalive_requests 1000000;
        limit_conn client 32;
        limit_conn_status 503;
        location / {{
            proxy_pass http://origin;
            proxy_http_version 1.1;
            proxy_set_header Host backend.test:8000;
            proxy_set_header Connection "";
            proxy_connect_timeout 1s;
            proxy_read_timeout 15s;
            proxy_send_timeout 15s;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_next_upstream off;
        }}
    }}
}}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy", type=Path)
    parser.add_argument("generator", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--protocol", choices=("http1", "http2"), required=True)
    parser.add_argument("--proxy-kind", choices=("cauce", "nginx", "haproxy", "pingora"), default="cauce")
    parser.add_argument("--strategy", choices=("compact", "sharded"), default="compact")
    parser.add_argument("--endpoints", type=int, choices=range(1, 1025), metavar="1..1024", default=1)
    parser.add_argument("--logs", action="store_true")
    parser.add_argument("--route-match", choices=("prefix", "regex"), default="prefix",
                        help="select the equivalent one-route HTTP/1 matching fixture")
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
    parser.add_argument("--matched", action="store_true", help="measure one rate between direct controls")
    parser.add_argument("--rates", type=int, nargs="+", required=True)
    parser.add_argument("--p99-ms", type=float, required=True)
    parser.add_argument("--p999-ms", type=float, required=True)
    args = parser.parse_args()
    if args.proxy_kind != "cauce" and (
        args.protocol != "http1" or args.workers != 1 or args.logs or args.endpoints != 1
        or args.strategy != "compact"
    ):
        parser.error("reference adapters qualify one-worker cleartext HTTP/1.1 with logs off")
    if args.matched and len(args.rates) != 1:
        parser.error("--matched requires exactly one offered rate")
    if args.route_match == "regex" and (args.proxy_kind != "cauce" or args.protocol != "http1"
                                       or args.endpoints != 1):
        parser.error("the regex fixture requires Cauce HTTP/1 with one endpoint")

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
    if args.proxy_kind == "nginx":
        config = root / "nginx.conf"
        config.write_text(nginx_config(listen, backend_port))
        launch = [str(args.proxy), "-e", "stderr", "-p", str(root) + "/", "-c", str(config)]
        readiness = lambda: http(listen, "/ready")
    elif args.proxy_kind == "haproxy":
        config = root / "haproxy.cfg"
        source = (Path(__file__).with_name("references") / "haproxy-clear.cfg").read_text()
        config.write_text(source.replace("0.0.0.0:8080", f"127.0.0.1:{listen}")
                          .replace("server fixture backend:8000", f"server fixture 127.0.0.1:{backend_port}"))
        launch = [str(args.proxy), "-db", "-f", str(config)]
        readiness = lambda: http(listen, "/ready")
    elif args.proxy_kind == "pingora":
        launch = [str(args.proxy), f"127.0.0.1:{listen}", f"127.0.0.1:{backend_port}"]
        readiness = lambda: http(listen, "/ready")
    else:
        source = proxy_config(args, listen, admin, backend_port, root / "access.jsonl", args.logs)
        if args.route_match == "regex":
            source = source.replace('path_prefix="/"', 'path_match="regex"\npath_pattern="^/"')
        config.write_text(endpoint_config(source, args.endpoints) if args.endpoints > 1 else source)
        launch = [str(args.proxy), str(config)]
        readiness = lambda: http(admin, "/ready")
    write_json(root / "fixture.json", {
        "proxy_sha256": digest(args.proxy), "backend_source_sha256": digest(fixture),
        "proxy_kind": args.proxy_kind,
        "proxy_cpus": args.proxy_cpus, "backend_cpu": args.backend_cpu, "client_cpu": args.client_cpu,
        "workers": args.workers, "logs": args.logs, "protocol": args.protocol,
        "strategy": args.strategy, "endpoints": args.endpoints, "route_match": args.route_match,
        "sampler_cpu": args.sampler_cpu, "concurrency": args.concurrency, "queue": args.queue,
    })
    target, control = root / "target.json", root / "control.json"
    configs = [
        {**native_spec(args, port), "concurrency": args.concurrency, "queue": args.queue}
        for port in (listen, backend_port)
    ]
    for path, spec in zip((target, control), configs):
        write_json(path, spec)
    backend_env = {**os.environ, "PORT": str(backend_port), "TLS": "0", "BENCH_H2_TLS": "0",
                   "BENCH_BIND": "127.0.0.1", "BENCH_STATS_PORT": str(stats_port)}
    proxy_env = {**os.environ, "TOKIO_WORKER_THREADS": str(args.workers),
                 "CAUCE_POOL_STRATEGY": args.strategy, "CAUCE_TRANSPORT_DIAGNOSTICS": "0"}
    with process(["taskset", "-c", str(args.backend_cpu), "node", str(fixture)],
                 root / "backend.log", backend_env) as backend:
        until(backend, lambda: http(stats_port, "/stats"), 5)
        proxy_cpus = ",".join(map(str, args.proxy_cpus))
        with process(["taskset", "-c", proxy_cpus, *launch],
                     root / "proxy.log", proxy_env) as proxy:
            until(proxy, readiness, 5)
            if args.matched:
                if args.sampler_cpu is not None:
                    os.sched_setaffinity(0, {args.sampler_cpu})
                prefix = ["taskset", "-c", ",".join(map(str, args.client_cpu))]
                control_spec = {**configs[1], "rate": args.rate * 2}
                limits = (args.p99_ms * 1e6, args.p999_ms * 1e6, .99, 0)
                before = run_cell(args.generator, root, control_spec, "control-before", prefix)
                if not passes(before, *limits):
                    return 2
                backend_before = json.loads(http(stats_port, "/stats")[0])
                measured = run_cell(args.generator, root, configs[0], "matched", prefix, proxy.pid)
                backend_after = json.loads(http(stats_port, "/stats")[0])
                after = run_cell(args.generator, root, control_spec, "control-after", prefix)
                completed = measured.get("successful", 0) + measured.get("warmup_successes", 0)
                qualified = (passes(measured, *limits) and passes(after, *limits)
                             and completed == backend_after["requests"] - backend_before["requests"])
                write_json(root / "matched.json", {"qualified": qualified, "target": measured,
                                                  "control_before": before, "control_after": after,
                                                  "backend_before": backend_before,
                                                  "backend_after": backend_after})
                return 0 if qualified else 2
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
