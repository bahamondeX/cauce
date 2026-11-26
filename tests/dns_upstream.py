#!/usr/bin/env python3
"""Exercise positive TTL changes over HTTP pools and Stream associations."""
import http.client
import json
import shutil
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


class UDP6(socketserver.UDPServer):
    address_family = socket.AF_INET6


class Datagram(socketserver.BaseRequestHandler):
    def handle(self):
        _, listener = self.request
        listener.sendto(self.server.marker, self.client_address)


class Rotation(unittest.TestCase):
    def launch(self, server):
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def origins(self, handler, udp=False):
        ipv4, ipv6 = (socketserver.UDPServer, UDP6) if udp else (TCP, TCP6)
        old = self.launch(ipv4(("127.0.0.1", 0), handler))
        new = ipv6(("::1", old.server_address[1]), handler, bind_and_activate=False)
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
        datagram_port = self.origins(Datagram, udp=True)
        listen, stream = port(), port()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            datagram = probe.getsockname()[1]
        config = settings()
        config.update(
            listen=f"127.0.0.1:{listen}", admin=f"127.0.0.1:{port()}",
            dns={"servers": [f"127.0.0.1:{dns.server_address[1]}"], "timeout_ms": 300},
            routes=[{"id": "dns", "host": "localhost", "path_prefix": "/", "upstreams": ["python"]}],
            stream={"upstreams": [{"id": "echo", "endpoints": [{"dns_address": f"echo.test:{echo_port}"}]},
                                  {"id": "datagram", "endpoints": [{"dns_address": f"udp.test:{datagram_port}"}]}],
                    "tcp": [{"listen": f"127.0.0.1:{stream}", "upstream": "echo"}],
                    "udp": [{"listen": f"127.0.0.1:{datagram}", "upstream": "datagram",
                             "options": {"idle_timeout": "8s", "session_timeout": "12s"}}]},
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
                with (socket.create_connection(("127.0.0.1", stream), timeout=2) as existing,
                      socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as prior):
                    def exchange(peer):
                        peer.settimeout(2)
                        peer.sendto(b"fixture", ("127.0.0.1", datagram))
                        return peer.recv(1)

                    existing.sendall(b"x")
                    self.assertEqual(existing.recv(1), b"4")
                    self.assertEqual(exchange(prior), b"4")
                    dns.family = socket.AF_INET6
                    time.sleep(1.15)
                    self.assertEqual(request(listen, "/")[2], b"6")
                    self.assertEqual(request(listen, "/")[2], b"6")
                    with socket.create_connection(("127.0.0.1", stream), timeout=2) as fresh:
                        fresh.sendall(b"x")
                        self.assertEqual(fresh.recv(1), b"6")
                    existing.sendall(b"x")
                    self.assertEqual(existing.recv(1), b"4")
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as fresh_udp:
                        self.assertEqual(exchange(fresh_udp), b"6")
                    self.assertEqual(exchange(prior), b"4")
            finally:
                process.terminate()
                process.wait(timeout=8)

    @unittest.skipUnless(shutil.which("node"), "requires the Node HTTP/2 fixture")
    def test_http2_does_not_lease_retired_transport_and_finishes_active_response(self):
        for strategy in ("compact", "sharded"):
            with self.subTest(pool=strategy), tempfile.TemporaryDirectory() as tmp:
                self.exercise_http2(Path(tmp), strategy)

    def exercise_http2(self, directory, strategy):
        from os import environ
        from contextlib import closing

        dns = self.launch(socketserver.UDPServer(("127.0.0.1", 0), Dns))
        dns.family = socket.AF_INET
        origin = directory / "origin.port"
        script = """
const http2 = require('node:http2'), fs = require('node:fs');
function backend(marker) {
  const server = http2.createServer();
  server.on('stream', (stream, headers) => {
    stream.respond({':status': 200});
    if (headers[':path'] === '/hold') {
      stream.write(marker);
      setTimeout(() => stream.end(marker), 3500);
    } else stream.end(marker);
  });
  return server;
}
const older = backend('4'), newer = backend('6');
older.listen(0, '127.0.0.1', () => {
  const port = older.address().port;
  newer.listen({port, host: '::1', ipv6Only: true}, () => {
    fs.writeFileSync(process.argv[1] + '.tmp', String(port));
    fs.renameSync(process.argv[1] + '.tmp', process.argv[1]);
    console.error('origin-ready');
  });
});
"""
        origin_log = directory / "origin.log"
        with origin_log.open("wb") as log, subprocess.Popen(
            ["node", "-e", script, origin], cwd=directory, stderr=log
        ) as backend:
            try:
                wait(origin_log, "origin-ready", backend)
                backend_port = int(origin.read_text())
                listen = port()
                config = settings()
                config["limits"].update(stream_idle_ms=6000, request_ms=8000)
                config.update(listen=f"127.0.0.1:{listen}", admin=f"127.0.0.1:{port()}",
                              dns={"servers": [f"127.0.0.1:{dns.server_address[1]}"], "timeout_ms": 350},
                              routes=[{"id": "dns", "host": "localhost", "path_prefix": "/", "upstreams": ["python"]}])
                config["upstreams"] = config["upstreams"][:1]
                config["upstreams"][0].update(address=f"h2.test:{backend_port}", protocol="http2")
                conf = directory / "cauce.yaml"
                conf.write_text(json.dumps({"version": 1, "proxy": config}))
                log_path = directory / "cauce.log"
                with log_path.open("wb") as errors, subprocess.Popen([BIN, conf], cwd=directory, stderr=errors,
                        env={**environ, "CAUCE_POOL_STRATEGY": strategy}) as proxy:
                    try:
                        wait(log_path, '"started"', proxy)
                        with closing(http.client.HTTPConnection("127.0.0.1", listen, timeout=6)) as client:
                            client.request("GET", "/hold", headers={"Host": "localhost"})
                            response = client.getresponse()
                            self.assertEqual(response.read(1), b"4")
                            dns.family = socket.AF_INET6
                            time.sleep(1.15)
                            self.assertEqual(request(listen, "/")[2], b"6")
                            self.assertEqual(response.read(), b"4")
                    finally:
                        proxy.terminate()
                        proxy.wait(timeout=8)
            finally:
                backend.terminate()
                backend.wait(timeout=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
