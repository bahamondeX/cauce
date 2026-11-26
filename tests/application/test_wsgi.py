#!/usr/bin/env python3
"""Native uWSGI and SCGI wire checks and optional independent differential."""
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import unittest

from test_application import (CAUCE, NGINX, TOKEN, ThreadUnix, app_config, call,
                              eventual, free_port, start_process, start_server, tcp)


class Wire(socketserver.BaseRequestHandler):
    def handle(self):
        with self.request.makefile("rb") as stream:
            if self.server.protocol == "uwsgi":
                one, lo, hi, two = stream.read(4)
                assert (one, two) == (7, 11)
                params = stream.read(lo + (hi << 8))
                fields = {}
                while params:
                    key_len = int.from_bytes(params[:2], "little")
                    key = params[2:2+key_len]
                    val_len = int.from_bytes(params[2+key_len:4+key_len], "little")
                    val = params[4+key_len:4+key_len+val_len]
                    params = params[4+key_len+val_len:]
                    assert key not in fields
                    fields[key] = val
            else:
                prefix = bytearray()
                while (byte := stream.read(1)) != b":":
                    assert byte and byte.isdigit() and len(prefix) < 6
                    prefix += byte
                parts = stream.read(int(prefix)).split(b"\0")
                assert parts[:1] == [b"CONTENT_LENGTH"] and parts[2:4] == [b"SCGI", b"1"]
                assert not parts[-1] and stream.read(1) == b","
                fields = dict(zip(parts[:-1:2], parts[1::2]))
                assert len(fields) * 2 == len(parts) - 1
            count = int(fields[b"CONTENT_LENGTH"])
            body = stream.read(count)
            assert len(body) == count and b"HTTP_PROXY" not in fields
            mode = fields[b"QUERY_STRING"]
            if mode == b"bad":
                self.request.sendall(b"Status: 201\r\nContent-Length: garbage\r\n\r\n")
            elif mode == b"eof":
                self.request.sendall(b"Status: 201\r\n")
            else:
                result = json.dumps([fields[b"PATH_INFO"].decode(), hashlib.sha256(body).hexdigest()]).encode()
                self.request.sendall(b"Status: 201 Created\r\nContent-Length: " +
                                     str(len(result)).encode() + b"\r\n\r\n" + result)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="c8-wsgi-")))

    def serve(self, protocol, backend, modifiers=None):
        port = free_port()
        config = app_config(port, free_port(), backend, keep=False)
        config["proxy"]["upstreams"][0]["protocol"] = protocol
        config["proxy"]["routes"][0]["application"] = {"request_buffer_bytes": 1048576, **(modifiers or {})}
        path = self.tmp / f"{port}.yaml"
        path.write_text(json.dumps(config))
        log = path.with_suffix(".log")
        start_process(self.stack, [CAUCE, str(path)], log, env={**os.environ, "CAUCE_ADMIN_TOKEN": TOKEN})
        try:
            eventual(lambda: '"event":"started"' in log.read_text())
        except AssertionError:
            self.fail(log.read_text())
        return port

    def wire(self, protocol):
        path = str(self.tmp / (protocol + ".sock"))
        server = start_server(self.stack, ThreadUnix, Wire, path=path)
        server.protocol = protocol
        modifiers = {"modifier1": 7, "modifier2": 11} if protocol == "uwsgi" else None
        port = self.serve(protocol, f"unix:{path}", modifiers)
        status, _, body = call(port, "/sub", "POST", iter([b"a", b"bc"]), {"Proxy": "http://bad.invalid"}, True)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), ["/sub", hashlib.sha256(b"abc").hexdigest()])
        for mode in ("bad", "eof"):
            self.assertEqual(call(port, "/?" + mode)[0], 502)
            import time
            time.sleep(0.12)
        self.assertEqual(call(port)[0], 201)

    def test_native_uwsgi_wire(self):
        self.wire("uwsgi")

    def test_native_scgi_wire(self):
        self.wire("scgi")

    def reference(self, protocol, backend):
        version = subprocess.run([NGINX, "-v"], capture_output=True, text=True, check=True).stderr
        self.assertIn("nginx/1.30.8\n", version)
        port = free_port()
        conf = self.tmp / f"{protocol}.conf"
        fields = {"REQUEST_METHOD": "$request_method", "QUERY_STRING": "$query_string",
                  "PATH_INFO": "$uri", "SERVER_NAME": "$server_name", "SERVER_PORT": "$server_port",
                  "SERVER_PROTOCOL": "$server_protocol", "CONTENT_TYPE": "$content_type"}
        fields |= {"CONTENT_LENGTH": "$content_length"} if protocol == "uwsgi" else {"SCGI": "1"}
        params = "\n".join(f"    {protocol}_param {k} {v};" for k, v in fields.items())
        conf.write_text(f"""
daemon off;
master_process off;
error_log stderr notice;
pid {self.tmp}/{protocol}.pid;
events {{ worker_connections 64; }}
http {{
 access_log off;
 client_max_body_size 2m;
 server {{
  listen 127.0.0.1:{port};
  server_name fixture.test;
  location / {{
    {protocol}_pass 127.0.0.1:{backend};
    {protocol}_buffering off;
{params}
  }}
 }}
}}
""")
        start_process(self.stack, [NGINX, "-p", str(self.tmp), "-c", str(conf)], conf.with_suffix(".log"))
        self.ready(port)
        return port

    @staticmethod
    def ready(port):
        def attempt():
            with tcp(port):
                return True
        eventual(attempt)

    def real(self, protocol, args):
        backend = free_port()
        container = subprocess.check_output([
            "docker", "run", "--rm", "-d", "--pull=never", "-p", f"127.0.0.1:{backend}:9000",
            "cauce-m8-wsgi", *args
        ], text=True).strip()
        self.stack.callback(subprocess.run, ["docker", "stop", "-t", "1", container], stdout=subprocess.DEVNULL)
        self.ready(backend)
        port = self.serve(protocol, f"127.0.0.1:{backend}")
        ports = [port] + ([self.reference(protocol, backend)] if NGINX else [])
        payload = (b"\xabchunk\0" * 102400) + b"end"
        for uri, method, body, expected in [
            ("/items?find=a", "GET", None, 201), ("/upload", "POST", payload, 201),
            ("/?redirect", "GET", None, 307), ("/?error", "GET", None, 500),
            ("/?slow", "GET", None, 200),
        ]:
            results = [call(target, uri, method, body) for target in ports]
            for code, _, entity in results:
                self.assertEqual(code, expected, (uri, entity))
            if len(results) == 2:
                self.assertEqual(results[0][2], results[1][2], uri)
            if body:
                self.assertEqual(json.loads(results[0][2])["body_sha256"], hashlib.sha256(body).hexdigest())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", "/?slow", headers={"Host": "fixture.test"})
        response = conn.getresponse()
        self.assertEqual(response.read(32768), b"x" * 32768)
        self.assertEqual(response.read(), b"y" * 32768)
        print(f"{protocol} cases 5, NGINX differential {len(ports) == 2}", flush=True)

    @unittest.skipUnless(os.environ.get("CAUCE_WSGI_FIXTURE"), "build tests/application/Dockerfile and set CAUCE_WSGI_FIXTURE=1")
    def test_uwsgi_reference(self):
        self.real("uwsgi", ["uwsgi", "--plugin", "python3", "--socket", "0.0.0.0:9000",
                            "--wsgi-file", "/fixture/wsgi_fixture.py", "--callable", "application",
                            "--die-on-term", "--master", "--processes", "1"])

    @unittest.skipUnless(os.environ.get("CAUCE_WSGI_FIXTURE"), "build tests/application/Dockerfile and set CAUCE_WSGI_FIXTURE=1")
    def test_scgi_reference(self):
        self.real("scgi", ["uwsgi", "--plugin", "python3", "--scgi-socket", "0.0.0.0:9000",
                           "--wsgi-file", "/fixture/wsgi_fixture.py", "--callable", "application",
                           "--die-on-term", "--master", "--processes", "1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
