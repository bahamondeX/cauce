#!/usr/bin/env python3
"""Real-socket Layer 4 regression and optional NGINX Stream differential."""
import contextlib
import http.client
import ipaddress
import os
from pathlib import Path
import signal
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
CAUCE = os.environ.get("CAUCE", str(ROOT / "target/debug/cauce"))
NGINX = os.environ.get("NGINX")
TOKEN = "stream-fixture-admin-token-local-only"
PREFIX = b"\r\n\r\n\x00\r\nQUIT\n"


def address(port, host="127.0.0.1"):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def free_port(family=socket.AF_INET, kind=socket.SOCK_STREAM):
    with socket.socket(family, kind) as sock:
        sock.bind(("::1" if family == socket.AF_INET6 else "127.0.0.1", 0))
        return sock.getsockname()[1]


def eventual(probe, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError("fixture did not reach expected state")


def tcp(port, host="127.0.0.1"):
    return socket.create_connection((host, port), timeout=2)


def recv_all(sock):
    data = bytearray()
    while chunk := sock.recv(64 << 10):
        data.extend(chunk)
    return bytes(data)


def send_half_close(port, data, host="127.0.0.1"):
    with tcp(port, host) as sock:
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        return recv_all(sock)


class Quiet:
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class ThreadTcp(Quiet, socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class ThreadUnix(Quiet, socketserver.ThreadingUnixStreamServer):
    pass


class ThreadUdp(Quiet, socketserver.ThreadingUDPServer):
    pass


class HalfClose(socketserver.BaseRequestHandler):
    def handle(self):
        payload = recv_all(self.request)
        self.request.sendall(self.server.marker + payload)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while chunk := self.request.recv(8192):
            self.request.sendall(chunk)


class Slow(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.server.hold.wait(4)


class Udp(socketserver.BaseRequestHandler):
    def handle(self):
        packet, sock = self.request
        if packet == b"amplify":
            sock.sendto(b"!" * 48, self.client_address)
            return
        if packet == b"repeat":
            for _ in range(10):
                sock.sendto(packet, self.client_address)
            return
        if packet == b"loss":
            return
        if packet == b"first":
            time.sleep(0.15)
        sock.sendto(packet, self.client_address)


class Dns(socketserver.BaseRequestHandler):
    def handle(self):
        packet, sock = self.request
        # One A question from an independent dig client. Echo its ID and question.
        end = packet.index(0, 12) + 5
        response = packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + packet[12:end]
        response += struct.pack("!HHHIH4s", 0xc00c, 1, 1, 60, 4, bytes([192, 0, 2, 42]))
        sock.sendto(response, self.client_address)


class UnambiguousTls(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.context.wrap_socket(self.request, server_side=True) as tls:
            tls.sendall(self.server.marker)


def start_server(stack, base, handler, host="127.0.0.1", path=None, port=0):
    if ":" in host:
        base = type("IPv6Server", (base,), {"address_family": socket.AF_INET6})
    server = stack.enter_context(base(path if path else (host, port), handler))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    stack.callback(thread.join)
    stack.callback(server.shutdown)
    return server


def start_process(stack, args, log, **kwargs):
    fd = stack.enter_context(open(log, "wb"))
    proc = subprocess.Popen(args, stdout=fd, stderr=fd, **kwargs)

    def stop():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    stack.callback(stop)
    return proc


def write_config(path, admin, extra):
    base = (ROOT / "examples/stream.toml").read_text().split("[[stream.upstreams]]", 1)[0]
    path.write_text(base.replace("127.0.0.1:9090", address(admin)).replace("shutdown_ms = 5000", "shutdown_ms = 600") + extra)


def group(name, *endpoints, balance="weighted_round_robin"):
    items = ", ".join("{" + endpoint + "}" for endpoint in endpoints)
    return f'\n[[stream.upstreams]]\nid = "{name}"\nbalance = "{balance}"\nendpoints = [{items}]\n'


def endpoint(port, host="127.0.0.1"):
    return f'address = "{address(port, host)}"'


def tcp_config(port, name, options="", host="127.0.0.1"):
    return f'\n[[stream.tcp]]\nlisten = "{address(port, host)}"\nupstream = "{name}"\n[stream.tcp.options]\n{options}\n'


def udp_config(port, name, options="", host="127.0.0.1"):
    return tcp_config(port, name, options, host).replace("stream.tcp", "stream.udp")


def tls_config(port, routes, default="default"):
    return f'''
[[stream.tls_passthrough]]
listen = "{address(port)}"
default_upstream = "{default}"
preread_timeout = "400ms"
preread_bytes = 2048
routes = {routes}
'''


def tls_client(port, name, context, fragment=False):
    with tcp(port) as transport:
        transport.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        conn = context.wrap_bio(incoming, outgoing, server_hostname=name)
        first = True
        while True:
            try:
                conn.do_handshake()
                transport.sendall(outgoing.read())
                break
            except ssl.SSLWantReadError:
                data = outgoing.read()
                if first and fragment:
                    original = data[5:]
                    assert len(original) == int.from_bytes(data[3:5], "big")
                    records = (data[:3] + len(payload).to_bytes(2, "big") + payload
                               for payload in (original[:11], original[11:42], original[42:]))
                    wire = b"".join(records)
                    for i in range(0, len(wire), 17):
                        transport.sendall(wire[i:i + 17])
                        time.sleep(0.001)
                else:
                    transport.sendall(data)
                first = False
                chunk = transport.recv(8192)
                if not chunk:
                    raise ConnectionError("TLS handshake ended early")
                incoming.write(chunk)
        while True:
            try:
                return conn.read()
            except ssl.SSLWantReadError:
                chunk = transport.recv(8192)
                if not chunk:
                    raise ConnectionError("TLS payload missing")
                incoming.write(chunk)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="c7-")))
        self.admin = free_port()
        self.config = self.tmp / "cauce.toml"

    def serve(self, config):
        write_config(self.config, self.admin, config)
        log = self.tmp / "cauce.log"
        proc = start_process(self.stack, [CAUCE, str(self.config)], log, env={**os.environ, "CAUCE_ADMIN_TOKEN": TOKEN})
        try:
            eventual(lambda: self.metric_text())
        except AssertionError:
            self.fail(log.read_text())
        return proc

    def metric_text(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.admin, timeout=1)
        try:
            conn.request("GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            return response.read().decode()
        finally:
            conn.close()

    def metric(self, label):
        return next(int(line.split()[1]) for line in self.metric_text().splitlines()
                    if line.startswith(f"cauce_{label} "))

    def udp(self, family=socket.AF_INET, ip=None):
        sock = self.stack.enter_context(socket.socket(family, socket.SOCK_DGRAM))
        if ip:
            sock.bind((ip, 0))
        sock.settimeout(1)
        return sock

    def test_tcp_half_close_ipv6_unix(self):
        path = str(self.tmp / "echo.sock")
        server = start_server(self.stack, ThreadUnix, HalfClose, path=path)
        server.marker = b"after EOF:"
        proxy_port = free_port(socket.AF_INET6)
        self.serve(group("echo", f'unix = "{path}"') + tcp_config(proxy_port, "echo", host="::1"))
        payload = bytes(range(256)) * 4096
        self.assertEqual(send_half_close(proxy_port, payload, "::1"), server.marker + payload)

    def test_tcp_weighted_consistent_and_failure(self):
        a = start_server(self.stack, ThreadTcp, HalfClose)
        b = start_server(self.stack, ThreadTcp, HalfClose)
        a.marker, b.marker = b"A", b"B"
        weighted, sticky, failover = (free_port() for _ in range(3))
        bad = free_port()
        config = group("weights", endpoint(a.server_address[1]) + ", weight = 3", endpoint(b.server_address[1]))
        config += group("sticky", endpoint(a.server_address[1]), endpoint(b.server_address[1]), balance="consistent_hash")
        config += group("failure", endpoint(bad) + ", weight = 3", endpoint(b.server_address[1]))
        config += tcp_config(weighted, "weights") + tcp_config(sticky, "sticky") + tcp_config(failover, "failure")
        self.serve(config)
        values = [send_half_close(weighted, b"x") for _ in range(12)]
        self.assertEqual((values.count(b"Ax"), values.count(b"Bx")), (9, 3))
        self.assertEqual(len({send_half_close(sticky, b"x") for _ in range(6)}), 1)
        with self.assertRaises((ConnectionResetError, BrokenPipeError)):
            if send_half_close(failover, b"x") == b"":
                raise ConnectionResetError
        self.assertEqual(send_half_close(failover, b"x"), b"Bx")
        self.assertEqual(self.metric("tcp_upstream_failures_total"), 1)

    def test_tcp_backpressure_and_cancellation(self):
        server = start_server(self.stack, ThreadTcp, Slow)
        server.hold = threading.Event()
        port = free_port()
        opts = 'idle_timeout = "250ms"\nsession_timeout = "2s"\nsend_buffer_bytes = 4096\nreceive_buffer_bytes = 4096\nmax_sessions = 1'
        self.serve(group("slow", endpoint(server.server_address[1])) + tcp_config(port, "slow", opts))
        with tcp(port) as client:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            client.setblocking(False)
            count = 0
            with self.assertRaises(BlockingIOError):
                while count < 16 << 20:
                    count += client.send(b" " * 65536)
            self.assertLess(count, 16 << 20)
            eventual(lambda: self.metric("tcp_sessions_active") == 1)
            with tcp(port):
                eventual(lambda: self.metric("tcp_sessions_rejected_total") == 1)
        # The origin never reads. The idle deadline releases the admission slot.
        eventual(lambda: self.metric("tcp_sessions_active") == 0)
        server.hold.set()

    def test_tcp_absolute_deadline_and_graceful_drain(self):
        backend = start_server(self.stack, ThreadTcp, Echo)
        port = free_port()
        initial = group("echo", endpoint(backend.server_address[1])) + tcp_config(
            port, "echo", 'idle_timeout = "300ms"\nsession_timeout = "550ms"')
        proc = self.serve(initial)
        with tcp(port) as session:
            for _ in range(3):
                session.sendall(b"x")
                self.assertEqual(session.recv(1), b"x")
                time.sleep(0.13)
            session.sendall(b"x")
            self.assertEqual(session.recv(1), b"x")
            self.assertEqual(session.recv(1), b"")
        with tcp(port) as session:
            session.sendall(b"before")
            self.assertEqual(session.recv(6), b"before")
            # A restart-only change rejects the whole new generation.
            write_config(self.config, self.admin, initial.replace("550ms", "900ms"))
            proc.send_signal(signal.SIGHUP)
            eventual(lambda: "reload_rejected" in (self.tmp / "cauce.log").read_text())
            session.sendall(b"still")
            self.assertEqual(session.recv(5), b"still")
            write_config(self.config, self.admin, initial)
            proc.send_signal(signal.SIGHUP)
            eventual(lambda: '"event":"reloaded"' in (self.tmp / "cauce.log").read_text())
            proc.terminate()
            time.sleep(0.05)
            with self.assertRaises(OSError):
                tcp(port).close()
            session.sendall(b"after")
            self.assertEqual(session.recv(5), b"after")
        self.assertEqual(proc.wait(timeout=2), 0)

    def test_proxy_versions_tlvs_and_trust_boundary(self):
        server = start_server(self.stack, ThreadTcp, HalfClose)
        server.marker = b""
        v1, v2, untrusted = (free_port() for _ in range(3))
        base = 'proxy_in = { trusted_peers = ["127.0.0.1/32"], max_header_bytes = 64, header_timeout = "120ms" }\n'
        config = group("echo", endpoint(server.server_address[1]))
        config += tcp_config(v1, "echo", base + 'proxy_out = "v1"')
        config += tcp_config(v2, "echo", base + 'proxy_out = "v2"')
        config += tcp_config(untrusted, "echo", base.replace("127.0.0.1/32", "192.0.2.0/24"))
        self.serve(config)
        line = b"PROXY TCP4 192.0.2.1 198.51.100.1 5324 443\r\n"
        self.assertEqual(send_half_close(v1, line + b"app"), line + b"app")
        pair = ipaddress.IPv6Address("2001:db8::1").packed + ipaddress.IPv6Address("2001:db8::2").packed
        raw = pair + struct.pack("!HH", 5324, 443)
        v2_base = PREFIX + b"\x21\x21"
        tlv = b"\x01\x00\x02h2"
        wire = v2_base + struct.pack("!H", len(raw) + len(tlv)) + raw + tlv
        canonical = v2_base + struct.pack("!H", len(raw)) + raw
        self.assertEqual(send_half_close(v2, wire + b"app"), canonical + b"app")
        local = PREFIX + b"\x20\xff\x00\x03abc"
        self.assertTrue(send_half_close(v1, local + b"app").startswith(b"PROXY TCP4 127.0.0.1 "))
        for port, invalid in [(untrusted, line), (v1, b"PROXY TCP4 192.0.2.1"),
                              (v1, b"PROXY " + b"A" * 108), (v2, v2_base + b"\xff\xff"),
                              (v2, wire[:-5] + b"\x20\x00\x09xy")]:
            try:
                result = send_half_close(port, invalid)
            except (ConnectionResetError, BrokenPipeError):
                result = b""
            self.assertEqual(result, b"")
        with tcp(v1) as idle:
            idle.sendall(b"P")
            self.assertEqual(idle.recv(1), b"")

    def tls_origins(self):
        key, cert = self.tmp / "tls.key", self.tmp / "tls.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-subj", "/CN=route.test", "-days", "1", "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        server_context.set_alpn_protocols(["test"])
        origins = [start_server(self.stack, ThreadTcp, UnambiguousTls) for _ in range(2)]
        for origin, marker in zip(origins, (b"route", b"default")):
            origin.context = server_context
            origin.marker = marker
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.check_hostname = False
        client_context.load_verify_locations(cert)
        return origins, client_context

    def test_tls_preread_fragmented_records_alpn_and_default(self):
        (route, default), ctx = self.tls_origins()
        port = free_port()
        config = group("route", endpoint(route.server_address[1])) + group("default", endpoint(default.server_address[1]))
        config += tls_config(port, '[{ sni = "Route.Test", alpn = "test", upstream = "route" }]')
        self.serve(config)
        ctx.set_alpn_protocols(["test"])
        self.assertEqual(tls_client(port, "route.test", ctx, fragment=True), b"route")
        self.assertEqual(tls_client(port, None, ctx), b"default")
        ctx.set_alpn_protocols(["different"])
        self.assertEqual(tls_client(port, "route.test", ctx), b"default")
        with tcp(port) as idle:
            idle.sendall(b"\x16")
            self.assertEqual(idle.recv(1), b"")
        with tcp(port) as malformed:
            malformed.sendall(b"not TLS at all")
            self.assertEqual(malformed.recv(1), b"")
        with tcp(port) as oversized:
            oversized.sendall(b"\x16\x03\x01\x10\x00" + b"\x01" * 2500)
            try:
                self.assertEqual(oversized.recv(1), b"")
            except ConnectionResetError:
                pass

    def test_udp_ipv6_loss_order_and_amplification(self):
        origin = start_server(self.stack, ThreadUdp, Udp, host="::1")
        port = free_port(socket.AF_INET6, socket.SOCK_DGRAM)
        self.serve(group("echo", endpoint(origin.server_address[1], "::1")) + udp_config(
            port, "echo", 'max_datagram_bytes = 64\nidle_timeout = "1s"', "::1"))
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as client:
            client.settimeout(0.5)
            client.connect(("::1", port))
            for packet in (b"first", b"second", b"loss"):
                client.send(packet)
            self.assertEqual(client.recv(128), b"second")
            self.assertEqual(client.recv(128), b"first")
            with self.assertRaises(socket.timeout):
                client.recv(128)
        # Fresh tuples have no leftover credit from the deliberately lost request.
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as client:
            client.settimeout(0.3)
            client.connect(("::1", port))
            for packet in (b"amplify", b"x" * 65):
                client.send(packet)
            with self.assertRaises(socket.timeout):
                client.recv(128)
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as client:
            client.settimeout(0.3)
            client.connect(("::1", port))
            client.send(b"repeat")
            self.assertEqual(client.recv(128), b"repeat")
            with self.assertRaises(socket.timeout):
                client.recv(128)
            client.send(b"")
            self.assertEqual(client.recv(128), b"")
        self.assertGreaterEqual(self.metric("udp_datagrams_dropped_total"), 11)

    def test_udp_peer_global_caps_expiration_and_origin_failure(self):
        origin = start_server(self.stack, ThreadUdp, Udp)
        good, global_cap, recover = (free_port(kind=socket.SOCK_DGRAM) for _ in range(3))
        offline = free_port(kind=socket.SOCK_DGRAM)
        config = group("echo", endpoint(origin.server_address[1])) + group("offline", endpoint(offline))
        config += udp_config(good, "echo", 'max_sessions = 2\nmax_sessions_per_ip = 1\nidle_timeout = "750ms"')
        config += udp_config(global_cap, "echo", 'max_sessions = 2\nmax_sessions_per_ip = 2\nidle_timeout = "750ms"')
        config += udp_config(recover, "offline", 'idle_timeout = "250ms"\nfailure_cooldown = "150ms"')
        self.serve(config)
        first, same, other, overflow = (self.udp(ip="127.0.0.1") for _ in range(4))
        for client, listener in ((first, good), (first, global_cap), (other, global_cap)):
            client.sendto(b"hello", ("127.0.0.1", listener))
            self.assertEqual(client.recv(64), b"hello")
        for client, listener in ((same, good), (overflow, global_cap)):
            client.settimeout(0.2)
            client.sendto(b"x", ("127.0.0.1", listener))
        for client in (same, overflow):
            with self.assertRaises(socket.timeout):
                client.recv(64)
        self.assertEqual(self.metric("udp_sessions_rejected_total"), 2)
        eventual(lambda: self.metric("udp_sessions_active") == 0)
        same.sendto(b"recovered", ("127.0.0.1", good))
        self.assertEqual(same.recv(64), b"recovered")
        first.sendto(b"offline", ("127.0.0.1", recover))
        eventual(lambda: self.metric("udp_upstream_failures_total") >= 1)
        start_server(self.stack, ThreadUdp, Udp, port=offline)
        time.sleep(0.2)
        first.sendto(b"online", ("127.0.0.1", recover))
        self.assertEqual(first.recv(64), b"online")

    def test_udp_drains_outstanding_reply(self):
        origin = start_server(self.stack, ThreadUdp, Udp)
        port = free_port(kind=socket.SOCK_DGRAM)
        proc = self.serve(group("echo", endpoint(origin.server_address[1])) + udp_config(port, "echo"))
        client = self.udp()
        client.sendto(b"first", ("127.0.0.1", port))
        eventual(lambda: self.metric("udp_sessions_active") == 1)
        proc.terminate()
        self.assertEqual(client.recv(128), b"first")
        self.assertEqual(proc.wait(timeout=2), 0)

    def test_udp_dns_with_dig(self):
        origin = start_server(self.stack, ThreadUdp, Dns)
        port = free_port(kind=socket.SOCK_DGRAM)
        self.serve(group("dns", endpoint(origin.server_address[1])) + udp_config(port, "dns"))
        result = subprocess.check_output(["dig", "@127.0.0.1", "-p", str(port), "+short", "+noedns",
                                          "+time=1", "+tries=1", "gateway.test", "A"], text=True, timeout=4)
        self.assertEqual(result.strip(), "192.0.2.42")

    @unittest.skipUnless(NGINX, "set NGINX to the pinned NGINX 1.30.8 binary")
    def test_nginx_shared_stream_behaviors(self):
        ver = subprocess.run([NGINX, "-V"], capture_output=True, text=True, check=True).stderr
        self.assertIn("nginx/1.30.8\n", ver)
        self.assertIn("--with-stream_ssl_preread_module", ver)
        print(ver.strip(), flush=True)
        a, b = [start_server(self.stack, ThreadTcp, HalfClose) for _ in range(2)]
        a.marker, b.marker = b"A:", b"B:"
        udp = start_server(self.stack, ThreadUdp, Udp)
        (tls_a, tls_b), ctx = self.tls_origins()
        a_port, b_port = a.server_address[1], b.server_address[1]
        udp_port = udp.server_address[1]
        tls_a_port, tls_b_port = tls_a.server_address[1], tls_b.server_address[1]
        cauce_tcp, nginx_tcp, cauce_proxy, nginx_proxy, cauce_tls, nginx_tls = (free_port() for _ in range(6))
        cauce_udp, nginx_udp = (free_port(kind=socket.SOCK_DGRAM) for _ in range(2))
        spec = group("balance", endpoint(a_port) + ", weight = 3", endpoint(b_port))
        spec += group("plain", endpoint(a_port)) + group("udp", endpoint(udp_port))
        spec += group("route", endpoint(tls_a_port)) + group("default", endpoint(tls_b_port))
        spec += tcp_config(cauce_tcp, "balance")
        spec += tcp_config(cauce_proxy, "plain", 'proxy_in = { trusted_peers = ["127.0.0.1/32"] }\nproxy_out = "v1"')
        spec += udp_config(cauce_udp, "udp") + tls_config(cauce_tls, '[{ sni = "route.test", upstream = "route" }]')
        self.serve(spec)
        nginx = self.tmp / "nginx.conf"
        nginx.write_text(f'''
daemon off;
master_process off;
pid {self.tmp / "nginx.pid"};
error_log stderr notice;
events {{ worker_connections 128; }}
stream {{
    upstream balance {{ server 127.0.0.1:{a_port} weight=3; server 127.0.0.1:{b_port}; }}
    map $ssl_preread_server_name $target {{
        route.test 127.0.0.1:{tls_a_port};
        default 127.0.0.1:{tls_b_port};
    }}
    server {{ listen 127.0.0.1:{nginx_tcp}; proxy_pass balance; proxy_half_close on; }}
    server {{
        listen 127.0.0.1:{nginx_proxy} proxy_protocol;
        set_real_ip_from 127.0.0.1;
        proxy_protocol on;
        proxy_half_close on;
        proxy_pass 127.0.0.1:{a_port};
    }}
    server {{
        listen 127.0.0.1:{nginx_udp} udp;
        proxy_pass 127.0.0.1:{udp_port};
        proxy_responses 1;
    }}
    server {{ listen 127.0.0.1:{nginx_tls}; ssl_preread on; proxy_pass $target; }}
}}
''')
        log = self.tmp / "nginx.log"
        start_process(self.stack, [NGINX, "-p", str(self.tmp), "-c", str(nginx)], log)
        try:
            eventual(lambda: "start" in log.read_text() or log.stat().st_size == 0, 0.5)
            eventual(lambda: send_half_close(nginx_tcp, b"ready").endswith(b"ready"))
        except AssertionError:
            self.fail(log.read_text())
        for port in (cauce_tcp, nginx_tcp):
            values = [send_half_close(port, b"payload") for _ in range(12)]
            self.assertEqual(values.count(b"A:payload"), 9)
            self.assertEqual(values.count(b"B:payload"), 3)
        for port in (cauce_proxy, nginx_proxy):
            payload = b"PROXY TCP4 192.0.2.1 198.51.100.1 5324 443\r\n"
            # NGINX sends its local destination, so compare the forwarded source
            # and application body while preserving each implementation's metadata.
            wire = send_half_close(port, payload + b"request")
            header, _, request = wire.partition(b"\r\n")
            self.assertEqual(header.split()[2], b"192.0.2.1")
            self.assertEqual(header.split()[4], b"5324")
            self.assertEqual(request, b"request")
        for port in (cauce_udp, nginx_udp):
            client = self.udp()
            for packet in (b"one", b"two packets are separate"):
                client.sendto(packet, ("127.0.0.1", port))
                self.assertEqual(client.recv(128), packet)
        for port in (cauce_tls, nginx_tls):
            self.assertEqual(tls_client(port, "route.test", ctx), b"route")
            self.assertEqual(tls_client(port, None, ctx), b"default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
