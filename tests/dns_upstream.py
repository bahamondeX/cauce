#!/usr/bin/env python3
"""Exercise a positive TTL change through real HTTP/1 pool and Stream sockets."""
import json
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from content_handlers import request, settings, wait
from integration import BIN, port


class Dns(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        end = 12
        while data[end]:
            end += 1 + data[end]
        end += 1
        kind = struct.unpack_from("!H", data, end)[0]
        active = self.server.family
        payload = None
        if active == socket.AF_INET and kind == 1:
            payload = socket.inet_pton(active, "127.0.0.1")
        if active == socket.AF_INET6 and kind == 28:
            payload = socket.inet_pton(active, "::1")
        response = data[:2] + struct.pack("!5H", 0x8180, 1, int(payload is not None), 0, 0)
        response += data[12:end + 4]
        if payload:
            response += struct.pack("!HHH", 0xC00C, kind, 1)
            response += struct.pack("!IH", 1, len(payload)) + payload
        sock.sendto(response, self.client_address)


class Http(socketserver.BaseRequestHandler):
    def handle(self):
        with self.request.makefile("rb") as data:
            while data.readline():
                while (header := data.readline()) and header != b"\r\n":
                    pass
                if not header:
                    return
                marker = self.server.marker
                try:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\n" + marker)
                except OSError:
                    return


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while self.request.recv(1):
            try:
                self.request.sendall(self.server.marker)
            except OSError:
                return


class TCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class TCP6(TCP):
    address_family = socket.AF_INET6


class Rotation(unittest.TestCase):
    def launch(self, server):
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def origins(self, handler):
        old = self.launch(TCP(("127.0.0.1", 0), handler))
        new = TCP6(("::1", old.server_address[1]), handler, bind_and_activate=False)
        new.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        new.server_bind()
        new.server_activate()
        self.launch(new)
        old.marker = b"4"
        new.marker = b"6"
        return old.server_address[1]

    def test_pool_and_stream_rotate_while_existing_session_continues(self):
        for strategy in ("compact", "sharded"):
            with self.subTest(pool=strategy), tempfile.TemporaryDirectory() as tmp:
                self.exercise(Path(tmp), strategy)

    def exercise(self, directory, strategy):
        from os import environ
        dns = self.launch(socketserver.UDPServer(("127.0.0.1", 0), Dns))
        dns.family = socket.AF_INET
        http_port = self.origins(Http)
        echo_port = self.origins(Echo)
        listen, stream = port(), port()
        config = settings()
        config.update(
            listen=f"127.0.0.1:{listen}", admin=f"127.0.0.1:{port()}",
            dns={"servers": [f"127.0.0.1:{dns.server_address[1]}"], "timeout_ms": 300},
            routes=[{"id": "dns", "host": "localhost", "path_prefix": "/", "upstreams": ["python"]}],
            stream={"upstreams": [{"id": "echo", "endpoints": [{"dns_address": f"echo.test:{echo_port}"}]}],
                    "tcp": [{"listen": f"127.0.0.1:{stream}", "upstream": "echo"}]},
        )
        config["upstreams"] = config["upstreams"][:1]
        config["upstreams"][0]["address"] = f"http.test:{http_port}"
        conf = directory / "cauce.yaml"
        conf.write_text(json.dumps({"version": 1, "proxy": config}))
        log = directory / "stderr"
        with log.open("w") as errors, subprocess.Popen(
            [BIN, conf], cwd=directory, stderr=errors, env={**environ, "CAUCE_POOL_STRATEGY": strategy},
        ) as process:
            try:
                wait(log, '"started"', process)
                self.assertEqual(request(listen, "/")[2], b"4")
                self.assertEqual(request(listen, "/")[2], b"4")
                with socket.create_connection(("127.0.0.1", stream), timeout=2) as existing:
                    existing.sendall(b"x")
                    self.assertEqual(existing.recv(1), b"4")
                    dns.family = socket.AF_INET6
                    time.sleep(1.15)
                    self.assertEqual(request(listen, "/")[2], b"6")
                    self.assertEqual(request(listen, "/")[2], b"6")
                    with socket.create_connection(("127.0.0.1", stream), timeout=2) as fresh:
                        fresh.sendall(b"x")
                        self.assertEqual(fresh.recv(1), b"6")
                    existing.sendall(b"x")
                    self.assertEqual(existing.recv(1), b"4")
            finally:
                process.terminate()
                process.wait(timeout=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
