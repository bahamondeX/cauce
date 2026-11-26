#!/usr/bin/env python3
"""Account for fixed-rate network and application workloads on prequalified hosts."""
import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
from time import monotonic_ns
import tomllib

from capacity import digest, write_json
from telemetry import metrics, process, process_stats, until

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = {"fastcgi", "uwsgi", "scgi"}


def allocate(kind):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def affinity(cpu):
    return ["taskset", "-c", str(cpu)] if cpu is not None else []


def prepare(args, include_proxy, listen, origin, admin):
    root = args.output
    protocol = args.protocol
    address = f"127.0.0.1:{listen}"
    backend = f"127.0.0.1:{origin}"
    if not include_proxy:
        return []
    if args.engine == "cauce":
        base = tomllib.loads((ROOT / "examples/stream.toml").read_text())
        base.update(admin=f"127.0.0.1:{admin}", upstreams=[], routes=[])
        base.pop("stream", None)
        if protocol == "http3":
            base["limits"]["clients"] = max(args.connections, 128)
            base["http3"] = {
                "listen": address, "max_connections": max(args.connections, 128),
                "max_streams": min(256, args.inflight),
                "max_field_bytes": 4096,
                "tls": {"cert_file": str(args.cert), "key_file": str(args.key)},
            }
            base["routes"] = [{
                "id": "payload", "host": "localhost", "path_prefix": "/",
                "content": {"type": "static", "root": str(root / "document")},
            }]
        elif protocol in APPLICATION:
            base["name"] = "bench_cauce"
            base["listen"] = address
            base["limits"].update(requests=args.inflight, connections=args.inflight,
                                  idle_connections=min(16, args.inflight), connecting=min(16, args.inflight))
            app = {}
            reuse = min(16, args.inflight) if protocol == "fastcgi" and args.fastcgi_keepalive else 0
            if protocol == "fastcgi":
                app = {"document_root": "/fixture", "script": "main.php",
                       "script_name": "/main.php", "keep_connection": bool(reuse)}
            base["upstreams"] = [{"id": "app", "address": backend, "host": "fixture.test",
                                  "isolation": "bench", "protocol": protocol, "connections": args.inflight,
                                  "idle_connections": reuse, "failure_backoff_ms": "100ms"}]
            base["routes"] = [{"id": "app", "host": "fixture.test", "path_prefix": "/",
                               "upstreams": ["app"], "application": app}]
        else:
            options = {"idle_timeout": "1s" if protocol == "udp" else "10s"}
            if protocol == "udp":
                options.update(max_sessions=8192, max_sessions_per_ip=8192,
                               max_datagram_bytes=args.size)
            else:
                options.update(max_sessions=max(1024, args.inflight))
            base["stream"] = {
                protocol: [{"listen": address, "upstream": "echo", "options": options}],
                "upstreams": [{"id": "echo", "endpoints": [{"address": backend}]}],
            }
        config = root / "cauce.yaml"
        write_json(config, {"version": 1, "proxy": base})
        return [str(args.proxy), str(config)]
    if args.engine == "nginx":
        if protocol == "http3":
            block = f"""http {{
    client_body_temp_path client_body;
    proxy_temp_path proxy_body;
    fastcgi_temp_path fastcgi_body;
    uwsgi_temp_path uwsgi_body;
    scgi_temp_path scgi_body;
    access_log off;
    server {{
        listen {address} quic;
        server_name localhost;
        quic_retry on;
        http3_max_concurrent_streams {min(256, args.inflight)};
        ssl_certificate {json.dumps(str(args.cert))};
        ssl_certificate_key {json.dumps(str(args.key))};
        root {json.dumps(str(root / "document"))};
    }}
}}"""
        elif protocol in APPLICATION:
            params = {
                "REQUEST_METHOD": "$request_method", "QUERY_STRING": "$query_string",
                "CONTENT_TYPE": "$content_type", "PATH_INFO": "$uri", "SCRIPT_NAME": "/main.php",
                "SERVER_NAME": "$server_name", "SERVER_PORT": "$server_port",
                "SERVER_PROTOCOL": "$server_protocol",
            }
            if protocol == "fastcgi":
                params.update(SCRIPT_FILENAME="/fixture/main.php", DOCUMENT_ROOT="/fixture", REDIRECT_STATUS="200")
            if protocol == "scgi":
                params["SCGI"] = "1"
            else:
                params["CONTENT_LENGTH"] = "$content_length"
            fields = "\n".join(f"{protocol}_param {key} {value};" for key, value in params.items())
            reduction = ""
            keep = ""
            if protocol == "fastcgi" and args.fastcgi_keepalive:
                reduction = f"keepalive {min(16, args.inflight)}; keepalive_timeout 10s; keepalive_time 1m;"
                keep = "fastcgi_keep_conn on;"
            block = f"""http {{
    client_body_temp_path client_body;
    fastcgi_temp_path fastcgi_body;
    uwsgi_temp_path uwsgi_body;
    scgi_temp_path scgi_body;
    access_log off;
    client_max_body_size 16m;
    upstream app {{ server {backend} max_conns={args.inflight}; {reduction} }}
    server {{ listen {address}; server_name fixture.test;
        location / {{
            {protocol}_pass app;
            {protocol}_next_upstream off;
            {protocol}_request_buffering off;
            {protocol}_buffering off;
            {keep}
            {fields}
        }}
    }}
}}"""
        else:
            options = ("proxy_half_close on; tcp_nodelay on; proxy_timeout 10s;"
                       if protocol == "tcp" else "proxy_responses 1; proxy_timeout 1s;")
            udp = " udp" if protocol == "udp" else ""
            block = f"stream {{ server {{ listen {address}{udp}; proxy_pass {backend}; {options} }} }}"
        config = root / "nginx.conf"
        config.write_text(f"""daemon off;
master_process off;
pid nginx.pid;
error_log stderr error;
events {{ worker_connections {max(1024, args.inflight * 2 + 64)}; }}
{block}
""")
        return [str(args.proxy), "-p", str(root) + "/", "-c", str(config)]
    config = root / "haproxy.cfg"
    config.write_text(f"""global
    nbthread 1
defaults
    mode tcp
    timeout client 10s
    timeout server 10s
    timeout connect 5s
frontend incoming
    bind {address}
    default_backend echo
backend echo
    server origin {backend}
""")
    return [str(args.proxy), "-db", "-f", str(config)]


async def tcp_offer(port, payload, args):
    start = monotonic_ns()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    connected = monotonic_ns() - start

    async def upload():
        for offset in range(0, len(payload), 64 * 1024):
            writer.write(payload[offset:offset + 64 * 1024])
            await writer.drain()
        if args.hold_ms:
            await asyncio.sleep(args.hold_ms / 1000)
        writer.write_eof()

    async def download():
        chunks = []
        while chunk := await reader.read(64 * 1024):
            chunks.append(chunk)
            if args.slow_read_ms:
                await asyncio.sleep(args.slow_read_ms / 1000)
        return b"".join(chunks)

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(upload())
            response = group.create_task(download())
        if response.result() != payload:
            raise ValueError("TCP echo mismatch")
        return connected
    finally:
        writer.close()


async def udp_offer(port, payload, pool):
    loop = asyncio.get_running_loop()
    sock = pool.get_nowait() if pool else udp_socket(port)
    try:
        await loop.sock_sendall(sock, payload)
        response = await loop.sock_recv(sock, len(payload) + 1)
        if response != payload:
            raise ValueError("UDP datagram mismatch")
        return None
    finally:
        if pool:
            pool.put_nowait(sock)
        else:
            sock.close()


def udp_socket(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.connect(("127.0.0.1", port))
    return sock


async def application_offer(port, args, upload, upload_hash):
    start = monotonic_ns()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    connected = monotonic_ns() - start
    try:
        query = f"mode=benchmark&size={args.size}&hold={args.hold_ms}"
        method = "POST" if upload else "GET"
        writer.write(f"{method} /?{query} HTTP/1.1\r\nHost: fixture.test\r\n"
                     f"Connection: close\r\nContent-Length: {len(upload)}\r\n"
                     "Content-Type: application/octet-stream\r\n\r\n".encode())
        for position in range(0, len(upload), 64 * 1024):
            writer.write(upload[position:position + 64 * 1024])
            await writer.drain()
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        status, *fields = head[:-4].split(b"\r\n")
        headers = dict((key.lower(), value.strip()) for key, value in (field.split(b":", 1) for field in fields))
        if (status.split(b" ", 2)[1:2] != [b"200"] or headers.get(b"x-request-sha256") != upload_hash
                or headers.get(b"content-length") != str(args.size).encode()):
            raise ValueError("application response status, length or upload hash mismatch")
        remaining = args.size
        while remaining:
            data = await reader.read(min(64 * 1024, remaining))
            if not data or data.count(b"x") != len(data):
                raise ValueError("application response content mismatch")
            remaining -= len(data)
            if args.slow_read_ms:
                await asyncio.sleep(args.slow_read_ms / 1000)
        if await reader.read(1):
            raise ValueError("application response exceeded declared length")
        return connected
    finally:
        writer.close()


async def sample(args, target, pid, admin, token):
    output = args.output
    raw = output / "offers.jsonl"
    active = set()
    peak_active = 0
    handshakes = []
    measurements = []
    payload = b"x" * args.size if args.protocol not in APPLICATION else b""
    upload = b"a" * args.request_size
    upload_hash = hashlib.sha256(upload).hexdigest().encode()
    async with AsyncExitStack() as stack:
        quic_clients = []
        pool = None
        if args.protocol == "http3":
            from aioquic.asyncio import connect
            from aioquic.h3.connection import H3_ALPN
            from aioquic.quic.configuration import QuicConfiguration
            sys.path.insert(0, str(ROOT / "tests/http3"))
            from quic_client import Client
            for _ in range(args.connections):
                config = QuicConfiguration(
                    is_client=True, alpn_protocols=H3_ALPN, server_name="localhost",
                )
                config.load_verify_locations(cafile=str(args.ca or args.cert))
                start = monotonic_ns()
                connection = await asyncio.wait_for(stack.enter_async_context(
                    connect("127.0.0.1", target, configuration=config, create_protocol=Client)
                ), 5)
                quic_clients.append(connection)
                handshakes.append(monotonic_ns() - start)
        elif args.protocol == "udp" and args.reuse_udp:
            pool = asyncio.Queue(args.inflight)
            for _ in range(args.inflight):
                sock = udp_socket(target)
                stack.callback(sock.close)
                pool.put_nowait(sock)

        output_stream = stack.enter_context(raw.open("w"))

        def emit(row):
            output_stream.write(json.dumps(row, separators=(",", ":")) + "\n")

        async def offer(sequence, scheduled, warmup):
            started = monotonic_ns()
            row = dict(seq=sequence, scheduled_ns=scheduled, started_ns=started,
                       warmup=warmup, status="ok")
            try:
                async with asyncio.timeout(args.timeout):
                    if args.protocol == "tcp":
                        row["connect_ns"] = await tcp_offer(target, payload, args)
                    elif args.protocol == "udp":
                        await udp_offer(target, sequence.to_bytes(8, "big") + payload[8:], pool)
                    elif args.protocol in APPLICATION:
                        row["connect_ns"] = await application_offer(target, args, upload, upload_hash)
                    else:
                        quic = quic_clients[sequence % len(quic_clients)]
                        stream = quic.request("/payload", authority=b"localhost")
                        try:
                            headers, body = await quic.response(stream)
                        except (asyncio.CancelledError, OSError, TimeoutError):
                            quic.cancel(stream)
                            quic.inbox.pop(stream, None)
                            raise
                        if headers.get(b":status") != b"200" or body != payload:
                            raise ValueError("HTTP/3 response mismatch")
            except TimeoutError:
                row["status"] = "timeout"
            except (OSError, EOFError, ValueError, asyncio.LimitOverrunError, ExceptionGroup) as error:
                row["status"] = type(error).__name__
            row["ended_ns"] = monotonic_ns()
            emit(row)

        async def phase(seconds, start_seq, warmup):
            nonlocal peak_active
            start = monotonic_ns() + 50_000_000
            for i in range(seconds * args.rate):
                scheduled = start + (i * 1_000_000_000) // args.rate
                await asyncio.sleep(max(0, (scheduled - monotonic_ns()) / 1e9))
                if len(active) >= args.inflight:
                    emit(dict(seq=start_seq+i, scheduled_ns=scheduled, started_ns=monotonic_ns(),
                              ended_ns=monotonic_ns(), warmup=warmup, status="generator_full"))
                    continue
                task = asyncio.create_task(offer(start_seq + i, scheduled, warmup))
                active.add(task)
                task.add_done_callback(active.discard)
                peak_active = max(peak_active, len(active))
            await asyncio.gather(*list(active))

        async def process_samples():
            while True:
                measurements.append(snapshot(pid))
                await asyncio.sleep(1)

        await phase(args.warmup, 0, True)
        before = snapshot(pid)
        before_metrics = metrics(admin, token) if token else {}
        sampling = asyncio.create_task(process_samples())
        try:
            await phase(args.seconds, args.warmup * args.rate, False)
        finally:
            sampling.cancel()
            await asyncio.gather(sampling, return_exceptions=True)
        after = snapshot(pid)
        after_metrics = metrics(admin, token) if token else {}
    write_json(output / "process.json", dict(before=before, after=after, samples=measurements,
                                             handshake_ns=handshakes, peak_inflight=peak_active))
    summary = audit(args, raw, before, after)
    for key in ("upstream_connections_created_total", "upstream_reused_total"):
        if key in before_metrics and key in after_metrics:
            summary[key] = after_metrics[key] - before_metrics[key]
    write_json(output / "summary.json", summary)


def snapshot(pid):
    result = process_stats(pid)
    if platform.system() == "Linux":
        result["fds"] = len(list((Path("/proc") / str(pid) / "fd").iterdir()))
    return result


def audit(args, raw, before, after):
    rows = [json.loads(line) for line in raw.read_text().splitlines()]
    measured = [row for row in rows if not row["warmup"]]
    expect = args.seconds * args.rate
    warm = args.warmup * args.rate
    accounted = sorted(row["seq"] for row in rows) == list(range(warm + expect))
    accounted &= sorted(row["seq"] for row in measured) == list(range(warm, warm + expect))
    successes = [row for row in measured if row["status"] == "ok"]
    bytes_per_success = (args.size + args.request_size if args.protocol in APPLICATION
                         else (2 if args.protocol != "http3" else 1) * args.size)
    combined = bytes_per_success * len(successes)
    status = {value: sum(1 for row in measured if row["status"] == value)
              for value in sorted({row["status"] for row in measured})}

    def p99(values):
        values = sorted(values)
        return values[(99 * len(values) + 99) // 100 - 1] / 1e6 if values else None

    jitter = p99(row["started_ns"] - row["scheduled_ns"] for row in measured)
    cpu = after["cpu_seconds"] - before["cpu_seconds"]
    return {
        "offers": len(measured), "accounted": accounted, "outcomes": status,
        "qualified_generator": accounted and "generator_full" not in status and jitter is not None and jitter <= args.jitter_ms,
        "all_offers_succeeded": len(successes) == expect,
        "successful_per_second": len(successes) / args.seconds,
        "scheduler_p99_ms": jitter,
        "latency_p99_ms": p99(row["ended_ns"] - row["scheduled_ns"] for row in successes),
        "connect_p99_ms": p99(row["connect_ns"] for row in successes if row.get("connect_ns") is not None),
        "observed_payload_bytes": combined, "proxy_cpu_seconds": cpu,
        "proxy_cpu_per_gib": cpu * (1 << 30) / combined if combined else None,
        "proxy_cpu_us_per_success": cpu * 1e6 / len(successes) if successes else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", choices=["tcp", "udp", "http3", *sorted(APPLICATION)])
    parser.add_argument("engine", choices=["direct", "cauce", "nginx", "haproxy"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--proxy", type=Path)
    parser.add_argument("--origin-port", type=int)
    parser.add_argument("--origin-identity", help="Pin the staged backend build/image and fixture hashes")
    parser.add_argument("--request-size", type=int, default=0)
    parser.add_argument("--fastcgi-keepalive", action="store_true")
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--inflight", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=1)
    parser.add_argument("--jitter-ms", type=float, default=10)
    parser.add_argument("--hold-ms", type=int, default=0)
    parser.add_argument("--slow-read-ms", type=int, default=0)
    parser.add_argument("--reuse-udp", action="store_true")
    parser.add_argument("--connections", type=int, default=2)
    parser.add_argument("--cpus", type=int, nargs=3, metavar=("PROXY", "GENERATOR", "ORIGIN"))
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--ca", type=Path)
    args = parser.parse_args()
    proxy = args.engine != "direct"
    if proxy != bool(args.proxy) or (args.engine == "haproxy" and args.protocol != "tcp"):
        parser.error("Specify the reference binary for proxy modes. HAProxy applies to TCP.")
    if args.protocol == "http3" and (not proxy or not args.cert or not args.key):
        parser.error("HTTP/3 requires a proxy and TLS certificate/key")
    if not (1 <= args.rate <= 100_000 and 1 <= args.seconds <= 3600 and 0 <= args.warmup <= 3600
            and 1 <= args.inflight <= 1024 and 0 < args.timeout <= 60 and 0 < args.jitter_ms <= 10
            and 1 <= args.connections <= 128 and args.size > 0
            and args.hold_ms >= 0 and args.slow_read_ms >= 0):
        parser.error("Invalid workload bounds")
    if args.protocol == "udp" and not 8 <= args.size <= 8192:
        parser.error("UDP size must be 8–8192 bytes in the matched fixtures")
    if args.protocol in APPLICATION:
        if (not proxy or args.origin_port not in range(1, 65536) or not args.origin_identity
                or not 0 <= args.request_size <= 16 * 1024 * 1024 or args.size > 16 * 1024 * 1024
                or args.hold_ms > 1000):
            parser.error("Application workloads require a prestarted loopback backend, identity and valid bounds")
    elif args.origin_port or args.origin_identity or args.request_size:
        parser.error("Application origin and upload settings require an application protocol")
    if args.fastcgi_keepalive and args.protocol != "fastcgi":
        parser.error("Upstream keepalive applies to FastCGI")
    if (args.size + args.request_size) * args.inflight > 512 * 1024 * 1024 or (args.seconds + args.warmup) * args.rate > 2_000_000:
        parser.error("Generator in-flight payload or raw offer budget exceeded")
    if args.cpus and (platform.system() != "Linux" or len(set(args.cpus)) != 3):
        parser.error("CPU isolation requires three distinct Linux logical CPUs")
    for key in ("output", "proxy", "cert", "key", "ca"):
        if value := getattr(args, key):
            setattr(args, key, value.resolve())
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "logs").mkdir()
    if args.protocol == "http3":
        document = args.output / "document"
        document.mkdir()
        (document / "payload").write_bytes(b"x" * args.size)
    kind = socket.SOCK_STREAM if args.protocol == "tcp" or args.protocol in APPLICATION else socket.SOCK_DGRAM
    origin, listen, admin = args.origin_port or allocate(kind), allocate(kind), allocate(socket.SOCK_STREAM)
    while (proxy and listen == origin) or admin in {listen, origin}:
        listen, admin = allocate(kind), allocate(socket.SOCK_STREAM)
    proxy_cpu, generator_cpu, origin_cpu = args.cpus or (None, None, None)
    if generator_cpu is not None:
        os.sched_setaffinity(0, {generator_cpu})
    cmd = prepare(args, proxy, listen, origin, admin)
    if args.engine in {"nginx", "haproxy"}:
        flag = "-V" if args.engine == "nginx" else "-vv"
        version = subprocess.check_output([str(args.proxy), flag], text=True, stderr=subprocess.STDOUT)
        (args.output / "proxy-version.log").write_text(version)
        if args.engine == "nginx" and "nginx/1.30.8\n" not in version:
            parser.error("The NGINX reference must be version 1.30.8")
    write_json(args.output / "environment.json", {
        "platform": platform.uname()._asdict(),
        "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "proxy_command": cmd,
        "proxy_sha256": digest(args.proxy) if args.proxy else None,
        "python": sys.version, "cpu_affinity": args.cpus,
    })
    with ExitStack() as stack:
        if args.protocol != "http3" and args.protocol not in APPLICATION:
            origin_log = args.output / "origin.log"
            backend = stack.enter_context(process(affinity(origin_cpu) + [sys.executable,
                str(Path(__file__).with_name("network-origin.py")), args.protocol, str(origin)], origin_log))
            until(backend, lambda: "ready" in origin_log.read_text(), 5)
        token = "benchmark-local-metrics-token-at-least-32" if args.engine == "cauce" and args.protocol in APPLICATION else None
        if proxy:
            log = args.output / "proxy.log"
            gateway = stack.enter_context(process(affinity(proxy_cpu) + cmd, log,
                env={**os.environ, "TOKIO_WORKER_THREADS": "1", **({"CAUCE_ADMIN_TOKEN": token} if token else {})}))
            until(gateway, lambda: gateway.poll() is None and (
                "started" in log.read_text() if args.engine == "cauce" else True), 5)
            # UDP and QUIC do not provide a TCP readiness probe. The measured
            # phase starts after the warm-up and retains every startup miss.
        else:
            gateway = backend
        (args.output / "sampled-pid.txt").write_text(f"{gateway.pid}\n")
        asyncio.run(sample(args, listen if proxy else origin, gateway.pid, admin, token))
    print((args.output / "summary.json").read_text().strip())


if __name__ == "__main__":
    main()
