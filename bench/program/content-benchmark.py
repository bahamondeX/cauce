#!/usr/bin/env python3
"""Record raw static/range/cache content offers through the native generator."""
import argparse
from contextlib import ExitStack
import http.server
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import tomllib

from capacity import audit, digest, write_json
from telemetry import available_port, process, process_stats, until

ROOT = Path(__file__).resolve().parents[2]


def origin(size):
    class Origin(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            with self.server.lock:
                self.server.requests += 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    payload = b"x" * size
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.requests = 0
    server.lock = threading.Lock()
    return server


def prepare_cauce(args, root, document, proxy_port, admin, upstream_port):
    config = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
    config.update(listen=f"127.0.0.1:{proxy_port}", admin=f"127.0.0.1:{admin}")
    config["logs"]["enabled"] = False
    route = {"id": "bench", "host": f"localhost:{proxy_port}", "path_prefix": "/"}
    if upstream_port:
        endpoint = config["upstreams"][0]
        endpoint["address"] = f"127.0.0.1:{upstream_port}"
        config["upstreams"] = [endpoint]
        route.update(upstreams=[endpoint["id"]], cache={"max_size":"32MiB", "max_entry_size":"4MiB",
                     "max_fills": 4, "ttl":"1h", "max_waiters":256, "vary":[]})
    else:
        config["upstreams"] = []
        route["content"] = {"type":"static", "root":str(document), "max_open": min(256, max(32, args.concurrency))}
    config["routes"] = [route]
    if args.cert:
        config["tls"] = {"cert_file":str(args.cert), "key_file":str(args.key)}
    output = root / "cauce.yaml"
    write_json(output, {"version":1, "proxy":config})
    return [str(args.proxy), str(output)]


def prepare_nginx(args, root, document, proxy_port, _admin, upstream_port):
    zone = ""
    if upstream_port:
        zone = 'proxy_cache_path "./cache" keys_zone=fixture:1m max_size=32m inactive=1h;'
        handler = f"""
            proxy_pass http://127.0.0.1:{upstream_port};
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_cache fixture;
            proxy_cache_key "$scheme|$http_host|$request_uri";
            proxy_cache_lock on;
            proxy_cache_valid 200 1h;
        """
    else:
        handler = f"root {json.dumps(str(document))};" # Keep sendfile separate from the portable fixture.
    h2 = "http2 on;" if args.protocol == "http2" else ""
    secure = " ssl" if args.cert else ""
    tls = f"ssl_certificate {json.dumps(str(args.cert))}; ssl_certificate_key {json.dumps(str(args.key))};" if args.cert else ""
    output = root / "nginx.conf"
    output.write_text(f"""
        error_log stderr error;
        pid nginx.pid;
        worker_processes {args.workers};
        events {{ worker_connections 1024; }}
        http {{
            access_log off;
            client_body_temp_path client_body;
            proxy_temp_path proxy_temp;
            fastcgi_temp_path fastcgi_temp;
            scgi_temp_path scgi_temp;
            uwsgi_temp_path uwsgi_temp;
            {zone}
            server {{
                listen 127.0.0.1:{proxy_port}{secure};
                server_name localhost;
                {h2}
                {tls}
                location / {{ {handler} }}
            }}
        }}
    """)
    return [str(args.proxy), "-p", str(root) + "/", "-c", str(output), "-g", "daemon off;"]


def prepare_varnish(args, root, _document, proxy_port, _admin, upstream_port):
    config = root / "fixture.vcl"
    config.write_text(f"""vcl 4.1;
backend default {{ .host = "127.0.0.1"; .port = "{upstream_port}"; }}
sub vcl_recv {{
    if (req.http.Authorization || req.http.Cookie || req.http.Range) {{ return (pass); }}
    if (req.method != "GET" && req.method != "HEAD") {{ return (pass); }}
    return (hash);
}}
sub vcl_backend_response {{
    if (beresp.status != 200 || beresp.http.Set-Cookie
        || beresp.http.Cache-Control !~ "(?i)public") {{
        set beresp.uncacheable = true;
        set beresp.ttl = 0s;
        return (deliver);
    }}
    set beresp.grace = 0s;
    set beresp.keep = 0s;
}}
""")
    return [str(args.proxy), "-F", "-n", str(root/"varnish"), "-a", f"127.0.0.1:{proxy_port}",
            "-f", str(config), "-s", "malloc,32m"]


def snapshot(parent):
    # Count process CPU plus any current NGINX workers. Linux resources can
    # be attributed only after affinity, scheduling and control qualification.
    if platform.system() != "Linux":
        return None
    root = Path("/proc") / str(parent)
    child_pids = (root / "task" / str(parent) / "children").read_text().split()
    stats = [process_stats(pid) for pid in [parent, *map(int, child_pids)]]
    return {
        "cpu_seconds": sum(row["cpu_seconds"] for row in stats),
        "summed_rss_kib": sum(row["rss_kib"] for row in stats),
        "pss_kib": sum(row["Pss"] for row in stats),
        "pids": [parent,*map(int, child_pids)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["static", "range", "cache-hot", "cache-cold"])
    parser.add_argument("implementation", choices=["cauce", "nginx", "varnish"])
    parser.add_argument("proxy", type=Path)
    parser.add_argument("generator", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--cpus", nargs=3, type=int, metavar=("PROXY","GENERATOR","ORIGIN"))
    parser.add_argument("--protocol", choices=["http1","http2"], default="http1")
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--ca", type=Path)
    args = parser.parse_args()

    cached = args.kind.startswith("cache")
    if args.implementation == "varnish" and (not cached or args.cert or args.protocol != "http1"):
        parser.error("the Varnish reference covers cleartext HTTP/1.1 cache fixtures")
    if not 1 <= args.size <= (4 if cached else 100) * 1024 * 1024:
        parser.error("payload is outside the fixture range")
    if min(args.rate, args.seconds, args.workers, args.concurrency) < 1 or args.concurrency > 256 or args.warmup < 0:
        parser.error("invalid offer window or resource limits")
    if sum(path is not None for path in (args.cert,args.key,args.ca)) not in (0,3):
        parser.error("TLS requires certificate, private key and fixture CA")
    if args.cpus and (platform.system() != "Linux" or len(set(args.cpus)) != 3
                      or not set(args.cpus).issubset(os.sched_getaffinity(0))):
        parser.error("pinning requires three distinct available Linux CPUs")
    for key in ("proxy","generator","output","cert","key","ca"):
        value = getattr(args,key)
        if value is not None:
            setattr(args,key,value.resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output
    document = root / "site"
    document.mkdir()
    content = document / "file.txt"
    file_size = args.size * (2 if args.kind == "range" else 1)
    with content.open("wb") as file:
        unit = b"x" * 1048576
        for _ in range(file_size // len(unit)):
            file.write(unit)
        file.write(unit[:file_size % len(unit)])
    proxy_port, admin = available_port(), available_port()
    env = {**os.environ, "TOKIO_WORKER_THREADS": str(args.workers)}
    with ExitStack() as stack:
        backend = None
        if cached:
            backend = origin(args.size)
            worker = threading.Thread(target=backend.serve_forever, daemon=True)
            worker.start()
            if args.cpus:
                os.sched_setaffinity(worker.native_id, {args.cpus[2]})
            stack.callback(backend.server_close)
            stack.callback(backend.shutdown)
        prepare = {"cauce":prepare_cauce, "nginx":prepare_nginx, "varnish":prepare_varnish}[args.implementation]
        command = prepare(args, root, document, proxy_port, admin, backend.server_port if backend else None)
        if args.cpus:
            command = ["taskset","-c",str(args.cpus[0]),*command]
        proxy = stack.enter_context(process(command, root / "proxy.log", env, group=True))
        def ready():
            import socket
            with socket.create_connection(("127.0.0.1", proxy_port), timeout=.25):
                return True
        until(proxy, ready, 5)
        if args.kind == "cache-cold":
            args.warmup = 0
        spec = {"url":f"{'https' if args.cert else 'http'}://localhost:{proxy_port}/file.txt",
                "host":f"localhost:{proxy_port}","protocol":args.protocol, "rate":args.rate,
                "seconds":args.seconds,"warmup_seconds":args.warmup,
                "concurrency":args.concurrency,"queue":args.concurrency * 4,
                "request_bytes":0,"response_bytes":args.size,"response_byte":ord("x")}
        if args.ca:
            spec["ca_file"] = str(args.ca)
        if args.kind == "range":
            spec["byte_range"] = [0,args.size - 1]
        settings = root / "load.json"
        write_json(settings,spec)
        offers, summary = root / "offers.jsonl",root / "offers.summary.json"
        before = snapshot(proxy.pid)
        generator_command = [args.generator, settings, offers, summary]
        if args.cpus:
            generator_command[:0] = ["taskset","-c",str(args.cpus[1])]
        with (root/"generator.log").open("w") as log:
            subprocess.run(generator_command, stdout=log, stderr=subprocess.STDOUT,
                           timeout=args.seconds+args.warmup+90, check=True)
        after = snapshot(proxy.pid)
        totals = json.loads(summary.read_text())
        report = audit(offers,totals,spec)
        if before and after:
            samples = report["successful"] + totals["warmup_successes"]
            cpu = after["cpu_seconds"]-before["cpu_seconds"]
            report["process_sample"] = {"before":before, "after":after,
                "cpu_us_per_success_including_warmup": (cpu * 1e6 / samples if samples else None)}
        report.update(implementation=args.implementation, kind=args.kind,
                      proxy_sha256=digest(args.proxy), generator_sha256=digest(args.generator),
                      origin_requests=backend.requests if backend else None, cpu_affinity=args.cpus,
                      host=dict(platform.uname()._asdict()),
                      classification="diagnostic until independently qualified")
        write_json(root/"audit.json",report)
    print(json.dumps(report,indent=2))
    return int(report["offered"] != report["successful"])


if __name__ == "__main__":
    raise SystemExit(main())
