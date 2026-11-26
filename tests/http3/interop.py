#!/usr/bin/env python3
"""Independent aioquic and Node HTTP/2 interoperability checks."""
import asyncio
import contextlib
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest

from aioquic.asyncio import connect
from aioquic.buffer import encode_uint_var
from aioquic.h3.connection import H3_ALPN
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamReset

from quic_client import Client

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/stream"))
from interop import CAUCE, NGINX, eventual, free_port, start_process, tcp  # noqa: E402



def cert(tmp, name):
    key, crt = tmp / f"{name}.key", tmp / f"{name}.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "1", "-subj", f"/CN={name}", "-addext", f"subjectAltName=DNS:{name}",
                    "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    return key, crt


def client(port, crt, name="route.test"):
    cfg = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, server_name=name)
    cfg.load_verify_locations(cafile=str(crt))
    return connect("127.0.0.1", port, configuration=cfg, create_protocol=Client)


class Interop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="c7-h3-")))
        self.named = cert(self.tmp, "route.test")
        self.default = cert(self.tmp, "default.test")
        self.quic = free_port(kind=socket.SOCK_DGRAM)
        self.https = free_port()
        self.cleartext = free_port()
        self.admin = free_port()
        self.origin = free_port()
        self.markers = self.tmp / "cancel.log"

    def launch(self, explicit=False, path_migration=False):
        start_process(self.stack, ["node", str(ROOT / "tests/http3/origin.mjs"), str(self.origin),
                                   str(self.markers)], self.tmp / "origin.log")
        base = (ROOT / "examples/cauce.toml").read_text().split("[limits]", 1)[1].split("[[upstreams]]", 1)[0]
        base = "[limits]" + base.replace("logs/access.jsonl", str(self.tmp / "access.jsonl"))
        default_key, default_cert = self.default
        named_key, named_cert = self.named
        config = self.tmp / "cauce.toml"
        tls = f'''cert_file = "{default_cert}"
key_file = "{default_key}"
[TLS_TABLE.sni."route.test"]
cert_file = "{named_cert}"
key_file = "{named_key}"
'''
        if explicit:
            tcp_settings = ""
            tls_settings = f'''
[[listeners]]
listen = "127.0.0.1:{self.cleartext}"

[[listeners]]
listen = "127.0.0.1:{self.https}"
[listeners.tls]
{tls.replace("TLS_TABLE", "listeners.tls")}
'''
            quic_tls = "[http3.tls]\n" + tls.replace("TLS_TABLE", "http3.tls")
        else:
            tcp_settings = f'listen = "127.0.0.1:{self.https}"'
            tls_settings = "[tls]\n" + tls.replace("TLS_TABLE", "tls")
            quic_tls = ""
        config.write_text(f'''name = "cauce"
{tcp_settings}
admin = "127.0.0.1:{self.admin}"
{base}
{tls_settings}
[http3]
listen = "127.0.0.1:{self.quic}"
advertise = true
path_migration = {str(path_migration).lower()}
max_streams = 8
max_field_bytes = 4096
idle_timeout = "3s"
connection_timeout = "10s"
{quic_tls}
[[upstreams]]
id = "h2"
protocol = "http2"
address = "127.0.0.1:{self.origin}"
host = "origin.test"
isolation = "interop"
connections = 4
idle_connections = 2
failure_backoff_ms = 250

[[routes]]
id = "static"
host = "route.test"
path_prefix = "/hello"
[routes.content]
type = "direct"
status = 200
body = "hello over QUIC"

[[routes]]
id = "default"
host = "default.test"
path_prefix = "/hello"
[routes.content]
type = "direct"
status = 200
body = "default certificate"

[[routes]]
id = "proxy"
host = "route.test"
path_prefix = "/proxy"
upstreams = ["h2"]
[routes.cache]
max_entries = 8
max_size = 65536
max_entry_size = 4096
ttl = "30s"
''')
        log = self.tmp / "cauce.log"
        self.proc = start_process(self.stack, [CAUCE, str(config)], log)
        try:
            eventual(lambda: self.ready(self.admin))
            eventual(lambda: self.ready(self.origin))
        except AssertionError:
            self.fail(log.read_text())
        self.config = config
        self.gateway_log = log

    @staticmethod
    def ready(port):
        with tcp(port):
            return True

    async def test_tls_sni_direct_response_and_alt_svc(self):
        self.launch(explicit=True)
        with tcp(self.cleartext) as plain:
            plain.sendall(b"GET /hello HTTP/1.1\r\nHost: route.test\r\nConnection: close\r\n\r\n")
            response = bytearray()
            while chunk := plain.recv(8192):
                response.extend(chunk)
            self.assertIn(b"hello over QUIC", response)
            self.assertNotIn(b"alt-svc:", response.lower())
        async with client(self.quic, self.named[1]) as h3:
            headers, data = await h3.response(h3.request("/hello"))
            self.assertEqual((headers[b":status"], data), (b"200", b"hello over QUIC"))
        async with client(self.quic, self.default[1], "default.test") as h3:
            headers, data = await h3.response(h3.request("/hello", authority=b"default.test"))
            self.assertEqual((headers[b":status"], data), (b"200", b"default certificate"))

        ctx = ssl.create_default_context(cafile=str(self.named[1]))
        with ctx.wrap_socket(tcp(self.https), server_hostname="route.test") as tls:
            tls.sendall(b"GET /hello HTTP/1.1\r\nHost: route.test\r\nConnection: close\r\n\r\n")
            response = bytearray()
            while chunk := tls.recv(8192):
                response.extend(chunk)
        self.assertIn(f'alt-svc: h3=":{self.quic}"; ma=300'.encode(), response.lower())
        await asyncio.sleep(0.1)
        self.assertIn('"protocol":"HTTP/3"', (self.tmp / "access.jsonl").read_text())

    async def test_bidi_trailers_multiplexing_and_cache(self):
        self.launch()
        async with client(self.quic, self.named[1]) as h3:
            stream = h3.request("/proxy/bidi", b"POST", end=False, headers=[(b"te", b"trailers")])
            h3.data(stream, b"one ")
            initial = await h3.event(stream)
            self.assertEqual(dict(initial.headers)[b":status"], b"200")
            data = await h3.event(stream)
            self.assertEqual(data.data, b"ONE ")
            results = await asyncio.gather(*(h3.response(h3.request("/hello")) for _ in range(4)))
            self.assertTrue(all(data == b"hello over QUIC" for _, data in results))
            h3.data(stream, b"two")
            h3.trailers(stream, [(b"x-upload-trailer", b"forwarded")])
            headers, payload = await h3.response(stream)
            self.assertEqual(payload, b"TWO")
            self.assertEqual(headers[b"x-origin-trailer"], b"forwarded")
            self.assertEqual(headers[b"x-origin-protocol"], b"h2")
            _, first = await h3.response(h3.request("/proxy/cache"))
            _, second = await h3.response(h3.request("/proxy/cache"))
            self.assertEqual((first, second), (b"origin 1", b"origin 1"))

    async def test_cancellation_releases_stalled_origin(self):
        self.launch()
        async with client(self.quic, self.named[1]) as h3:
            stream = h3.request("/proxy/cancel")
            await asyncio.to_thread(eventual, lambda: self.markers.exists() and "opened" in self.markers.read_text())
            h3.cancel(stream)
            await asyncio.to_thread(eventual, lambda: "closed" in self.markers.read_text(), 1)
            _, data = await h3.response(h3.request("/hello"))
            self.assertEqual(data, b"hello over QUIC")

    async def test_graceful_quic_shutdown_drains_open_response(self):
        self.launch()
        async with client(self.quic, self.named[1]) as h3:
            stream = h3.request("/proxy/drain")
            headers = await h3.event(stream)
            self.assertEqual(dict(headers.headers)[b":status"], b"200")
            prefix = await h3.event(stream)
            self.assertEqual(prefix.data, b"before-")
            self.proc.terminate()
            _, data = await h3.response(stream)
            self.assertEqual(data, b"after")
            self.assertEqual(await asyncio.to_thread(self.proc.wait, 5), 0)

    async def test_quic_connection_survives_udp_port_rebinding(self):
        self.launch(path_migration=True)
        async with client(self.quic, self.named[1]) as h3:
            _, data = await h3.response(h3.request("/hello"))
            self.assertEqual(data, b"hello over QUIC")
            original = h3._transport
            endpoint = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            endpoint.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            endpoint.bind(("::", 0))
            endpoint.setblocking(False)
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: h3, sock=endpoint)
            try:
                self.assertNotEqual(transport.get_extra_info("sockname")[1], original.get_extra_info("sockname")[1])
                _, data = await h3.response(h3.request("/hello"))
                self.assertEqual(data, b"hello over QUIC")
            finally:
                h3._transport = original
                transport.close()

    async def test_encoded_qpack_section_budget(self):
        self.launch()
        async with client(self.quic, self.named[1]) as h3:
            for _ in range(5):
                stream = h3.stream()
                # HEADERS declares a wire field section larger than the
                # configured bound and never sends a payload.
                h3._quic.send_stream_data(stream, encode_uint_var(1) + encode_uint_var(1 << 20))
                h3.transmit()
                event = await h3.event(stream)
                self.assertIsInstance(event, StreamReset)
                self.assertEqual(event.error_code, 0x107)
            _, data = await h3.response(h3.request("/hello"))
            self.assertEqual(data, b"hello over QUIC")

    async def test_decoded_qpack_section_budget(self):
        self.launch()
        async with client(self.quic, self.named[1]) as h3:
            large = h3.request("/hello", headers=[(b"x-many-bytes", b"a" * 2500), (b"x-more-bytes", b"b" * 2500)])
            try:
                headers, _ = await h3.response(large)
            except ConnectionError as error:
                self.assertIn("StreamReset", str(error))
                self.assertIsNone(h3.closed_event)
            else:
                self.assertEqual(headers[b":status"], b"431")
            _, data = await h3.response(h3.request("/hello"))
            self.assertEqual(data, b"hello over QUIC")

    async def test_reload_rejects_invalid_tls_then_rotates_new_handshakes(self):
        self.launch(explicit=True)
        async with client(self.quic, self.named[1]) as original:
            _, payload = await original.response(original.request("/hello"))
            self.assertEqual(payload, b"hello over QUIC")
            config = self.config.read_text()
            self.config.write_text(config.replace(str(self.named[0]), str(self.default[0])))
            self.proc.send_signal(signal.SIGHUP)
            await asyncio.to_thread(eventual, lambda: '"event":"reload_rejected"' in self.gateway_log.read_text())
            async with client(self.quic, self.named[1]) as same:
                _, payload = await same.response(same.request("/hello"))
                self.assertEqual(payload, b"hello over QUIC")
            rotation = self.tmp / "new"
            rotation.mkdir()
            rotated_key, rotated_cert = cert(rotation, "route.test")
            self.config.write_text(config.replace(str(self.named[0]), str(rotated_key))
                                   .replace(str(self.named[1]), str(rotated_cert)))
            self.proc.send_signal(signal.SIGHUP)
            await asyncio.to_thread(eventual, lambda: '"event":"reloaded"' in self.gateway_log.read_text())
            async with client(self.quic, rotated_cert) as new:
                _, payload = await new.response(new.request("/hello"))
                self.assertEqual(payload, b"hello over QUIC")
            _, payload = await original.response(original.request("/hello"))
            self.assertEqual(payload, b"hello over QUIC")

    @unittest.skipUnless(NGINX, "set NGINX to the pinned HTTP/3 reference build")
    async def test_nginx_shared_http3_get(self):
        self.launch()
        port = free_port(kind=socket.SOCK_DGRAM)
        key, crt = self.named
        (self.tmp / "hello").write_text("hello over QUIC")
        (self.tmp / "logs").mkdir()
        config = self.tmp / "nginx.conf"
        config.write_text(f'''
daemon off;
master_process off;
pid {self.tmp / "nginx.pid"};
error_log stderr notice;
events {{ worker_connections 128; }}
http {{
    access_log off;
    server {{
        listen 127.0.0.1:{port} quic;
        quic_retry on;
        ssl_certificate "{crt}";
        ssl_certificate_key "{key}";
        server_name route.test;
        root "{self.tmp}";
    }}
}}
''')
        log = self.tmp / "nginx.log"
        proc = start_process(self.stack, [NGINX, "-p", str(self.tmp), "-c", str(config)], log)
        await asyncio.sleep(0.1)
        self.assertIsNone(proc.poll(), log.read_text())
        for target in (port, self.quic):
            async with asyncio.timeout(5):
                async with client(target, crt) as h3:
                    headers, body = await h3.response(h3.request("/hello"))
                    self.assertEqual((headers[b":status"], body), (b"200", b"hello over QUIC"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
