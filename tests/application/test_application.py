#!/usr/bin/env python3
"""FastCGI wire regression, real PHP-FPM and optional NGINX differential."""
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/stream"))
from interop import CAUCE, NGINX, TOKEN, ThreadTcp, ThreadUnix, eventual, free_port, start_process, start_server, tcp

PHP_IMAGE = "php@sha256:d39ec536545f1f698dc971cd2a0a77424867a27a992dd43b8e82d5cf112b02cb"
PHP = os.environ.get("CAUCE_PHP_FIXTURE")
FIXTURE = ROOT / "tests/application/main.php"


def record(kind, payload=b"", padding=b""):
    return struct.pack("!BBHHBB", 1, kind, 1, len(payload), len(padding), 0) + payload + padding


def receive(stream):
    prefix = stream.read(8)
    if not prefix:
        return None
    if len(prefix) != 8:
        raise EOFError("short FastCGI record")
    version, kind, request, length, padding, _ = struct.unpack("!BBHHBB", prefix)
    if version != 1 or request != 1:
        raise ValueError("unexpected FastCGI record")
    data = stream.read(length)
    stream.read(padding)
    if len(data) != length:
        raise EOFError("short FastCGI payload")
    return kind, data


def decode_params(params):
    result = {}
    def length():
        nonlocal params
        lead = params[0]
        size = 4 if lead & 128 else 1
        count = int.from_bytes(params[:size], "big") & 0x7fffffff
        params = params[size:]
        return count
    while params:
        key, val = length(), length()
        name = params[:key].decode()
        if name in result:
            raise ValueError("duplicate CGI environment key")
        result[name] = params[key:key+val].decode()
        params = params[key+val:]
    return result


class FastCGI(socketserver.BaseRequestHandler):
    def handle(self):
        with self.request.makefile("rb") as stream:
            while entry := receive(stream):
                assert entry[0] == 1 and entry[1][:2] == b"\x00\x01"
                keep = entry[1][2] == 1
                params, body = bytearray(), bytearray()
                while True:
                    typ, data = receive(stream)
                    if typ == 4:
                        params.extend(data)
                    if typ == 5:
                        body.extend(data)
                        if not data:
                            break
                fields = decode_params(params)
                self.server.exchanges.append((fields, len(body), self.request.getsockname()))
                mode = fields["QUERY_STRING"]
                if mode == "bad-record":
                    self.request.sendall(b"\x02" + record(6, b"bad")[1:])
                    return
                if mode == "noheaders":
                    self.request.sendall(record(6, b"X-Test: pending") + record(3, bytes(8)))
                    return
                if mode == "bad-head":
                    self.request.sendall(record(6, b"Status: 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"))
                    return
                if mode == "short":
                    self.request.sendall(record(6, b"Content-Length: 4\r\n\r\nA") + record(3, bytes(8)))
                    return
                if mode == "aborted":
                    self.request.sendall(record(6, b"Content-Type: text/plain\r\n\r\nstart"))
                    self.server.aborted.wait(2)
                    try:
                        self.request.sendall(record(6, b"end"))
                        self.request.settimeout(2)
                        if self.request.recv(1) == b"":
                            self.server.closed.set()
                    except OSError:
                        self.server.closed.set()
                    return
                response = json.dumps({"fields": fields, "bytes": len(body), "body": hashlib.sha256(body).hexdigest()}).encode()
                head = b"Status: 201 Created\r\nSet-Cookie: a=b\r\nSet-Cookie: c=d\r\nContent-Length: "
                self.request.sendall(record(7, b"redacted diagnostic") + record(6, head, b"\0\0"))
                self.request.sendall(record(6, str(len(response)).encode() + b"\r\n\r\n" + response))
                self.request.sendall(record(6) + record(3, bytes(8)))
                if not keep:
                    return


def app_config(port, admin, backend, *, script_root="/fixture", script="main.php", buffer=0, keep=True):
    config = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
    config.update(listen=f"127.0.0.1:{port}", admin=f"127.0.0.1:{admin}")
    config["logs"]["enabled"] = False
    config["upstreams"] = [{
        "id": "app", "address": backend, "host": "backend.test", "isolation": "fixture",
        "protocol": "fastcgi", "connections": 8, "idle_connections": 8 if keep else 0,
        "failure_backoff_ms": "100ms",
    }]
    application = {"document_root": script_root, "script": script, "script_name": "/main.php",
                   "keep_connection": keep}
    if buffer:
        application["request_buffer_bytes"] = buffer
    config["routes"] = [{"id": "app", "host": "fixture.test", "path_prefix": "/",
                         "upstreams": ["app"], "application": application}]
    return {"version": 1, "proxy": config}


def call(port, uri="/", method="GET", body=None, headers=None, chunked=False):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, uri, body, {"Host": "fixture.test", **(headers or {})}, encode_chunked=chunked)
        result = conn.getresponse()
        return result.status, result.getheaders(), result.read()
    finally:
        conn.close()


class FastCGITests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="c8-")))
        self.port = free_port()
        self.admin = free_port()
        self.config = self.tmp / "cauce.yaml"

    def serve(self, backend, **options):
        self.config.write_text(json.dumps(app_config(self.port, self.admin, backend, **options)))
        log = self.tmp / "cauce.log"
        proc = start_process(self.stack, [CAUCE, str(self.config)], log, env={**os.environ, "CAUCE_ADMIN_TOKEN": TOKEN})
        try:
            eventual(lambda: '"event":"started"' in log.read_text())
        except AssertionError:
            self.fail(log.read_text())
        return proc

    def metric(self, name):
        conn = http.client.HTTPConnection("127.0.0.1", self.admin, timeout=2)
        try:
            conn.request("GET", "/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
            rows = conn.getresponse().read().decode().splitlines()
        finally:
            conn.close()
        return int(next(line.split()[1] for line in rows if line.startswith(f"cauce_{name} ")))

    def fixture(self, unix=False):
        if unix:
            path = str(self.tmp / "fpm.sock")
            server = start_server(self.stack, ThreadUnix, FastCGI, path=path)
            backend = f"unix:{path}"
        else:
            server = start_server(self.stack, ThreadTcp, FastCGI)
            backend = f"127.0.0.1:{server.server_address[1]}"
        server.exchanges = []
        server.aborted = threading.Event()
        server.closed = threading.Event()
        return server, backend

    def test_unix_reuse_environment_and_request_header_injection(self):
        server, backend = self.fixture(unix=True)
        self.serve(backend)
        sample = (b"abcdefg" * 102400) + b"end"
        request = {"Script-Filename": "/danger.php", "Remote-Addr": "192.0.2.10",
                   "Proxy": "http://invalid.test", "Script_Filename": "/alias.php",
                   "X-Forwarded-For": "192.0.2.9", "Content-Type": "application/octet-stream"}
        status, headers, data = call(self.port, "/outside/evil.php?a=%00", "POST", sample, request)
        self.assertEqual(status, 201)
        self.assertEqual([v for k, v in headers if k.lower() == "set-cookie"], ["a=b", "c=d"])
        result = json.loads(data)
        self.assertEqual(result["body"], hashlib.sha256(sample).hexdigest())
        fields = result["fields"]
        self.assertEqual(fields["SCRIPT_FILENAME"], "/fixture/main.php")
        self.assertEqual(fields["SCRIPT_NAME"], "/main.php")
        self.assertEqual(fields["QUERY_STRING"], "a=%00")
        self.assertEqual(fields["REQUEST_METHOD"], "POST")
        self.assertEqual(fields["REMOTE_ADDR"], "127.0.0.1")
        self.assertEqual(fields["HTTP_X_FORWARDED_FOR"], "127.0.0.1")
        self.assertEqual(fields["HTTP_SCRIPT_FILENAME"], "/danger.php")
        self.assertNotIn("HTTP_PROXY", fields)
        self.assertEqual(call(self.port)[0], 201)
        self.assertEqual(self.metric("upstream_connections_created_total"), 1)
        self.assertEqual(self.metric("upstream_reused_total"), 1)

    def test_bounded_chunked_request_and_opt_out(self):
        _, backend = self.fixture()
        self.serve(backend, buffer=4096)
        status, _, data = call(self.port, "/", "POST", iter((b"a" * 31, b"b" * 9)), chunked=True)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(data)["bytes"], 40)
        status, _, _ = call(self.port, "/", "POST", iter((b"a" * 5000,)), chunked=True)
        self.assertEqual(status, 413)

    def test_unknown_length_fails_before_app_connect(self):
        server, backend = self.fixture()
        self.serve(backend, keep=False)
        status, _, _ = call(self.port, "/", "POST", iter((b"chunk",)), chunked=True)
        self.assertEqual(status, 411)
        self.assertEqual(server.exchanges, [])

    def test_bad_and_truncated_response_cannot_reenter_pool(self):
        _, backend = self.fixture()
        self.serve(backend)
        for mode in ("bad-record", "noheaders", "bad-head"):
            self.assertEqual(call(self.port, "/?" + mode)[0], 502)
            eventual(lambda: self.metric("upstream_connections_in_use") == 0)
            time.sleep(0.11)
        with self.assertRaises((http.client.IncompleteRead, http.client.RemoteDisconnected)):
            call(self.port, "/?short")
        eventual(lambda: self.metric("upstream_connections_in_use") == 0)
        self.assertEqual(self.metric("upstream_connections_idle"), 0)
        self.assertEqual(call(self.port)[0], 201)

    def test_cancellation_closes_application_connection(self):
        server, backend = self.fixture()
        self.serve(backend)
        with tcp(self.port) as connection:
            connection.sendall(b"GET /?aborted HTTP/1.1\r\nHost: fixture.test\r\n\r\n")
            self.assertIn(b"200", connection.recv(2048).split(b"\r\n", 1)[0])
        server.aborted.set()
        self.assertTrue(server.closed.wait(3))
        eventual(lambda: self.metric("upstream_connections_in_use") == 0)

    def test_reload_rejects_path_escape(self):
        _, backend = self.fixture()
        proc = self.serve(backend)
        cfg = json.loads(self.config.read_text())
        cfg["proxy"]["routes"][0]["application"]["script"] = "../outside.php"
        self.config.write_text(json.dumps(cfg))
        import signal
        proc.send_signal(signal.SIGHUP)
        eventual(lambda: "reload_rejected" in (self.tmp / "cauce.log").read_text())
        self.assertEqual(json.loads(call(self.port)[2])["fields"]["SCRIPT_FILENAME"], "/fixture/main.php")

    @unittest.skipUnless(PHP, "set CAUCE_PHP_FIXTURE=1 to run pinned PHP-FPM")
    def test_php_fpm_and_optional_nginx(self):
        fpm_port = free_port()
        container = subprocess.check_output([
            "docker", "run", "--rm", "-d", "--pull=never", "-p", f"127.0.0.1:{fpm_port}:9000",
            "-v", f"{FIXTURE}:/fixture/main.php:ro", PHP_IMAGE,
        ], text=True).strip()
        self.stack.callback(subprocess.run, ["docker", "stop", "-t", "1", container], stdout=subprocess.DEVNULL, check=True)
        print(subprocess.check_output(["docker", "exec", container, "php-fpm", "-v"], text=True).splitlines()[0], flush=True)
        def ready(port):
            with tcp(port):
                return True
        eventual(lambda: ready(fpm_port))
        self.serve(f"127.0.0.1:{fpm_port}", buffer=2 * 1024 * 1024)
        ports = [self.port]
        if NGINX:
            version = subprocess.run([NGINX, "-V"], capture_output=True, text=True, check=True).stderr
            self.assertIn("nginx/1.30.8\n", version)
            ref = free_port()
            conf = self.tmp / "nginx.conf"
            conf.write_text(f"""
daemon off;
master_process off;
error_log stderr notice;
pid {self.tmp}/nginx.pid;
events {{ worker_connections 64; }}
http {{
  access_log off;
  client_max_body_size 2m;
  upstream fpm {{ server 127.0.0.1:{fpm_port}; keepalive 4; }}
  server {{
    listen 127.0.0.1:{ref};
    server_name fixture.test;
    location / {{
      fastcgi_pass fpm;
      fastcgi_keep_conn on;
      fastcgi_param SCRIPT_FILENAME /fixture/main.php;
      fastcgi_param SCRIPT_NAME /main.php;
      fastcgi_param DOCUMENT_ROOT /fixture;
      fastcgi_param QUERY_STRING $query_string;
      fastcgi_param REQUEST_METHOD $request_method;
      fastcgi_param CONTENT_TYPE $content_type;
      fastcgi_param CONTENT_LENGTH $content_length;
      fastcgi_param SERVER_NAME $server_name;
      fastcgi_param REMOTE_ADDR $remote_addr;
      fastcgi_param REQUEST_URI $request_uri;
      fastcgi_param REDIRECT_STATUS 200;
      fastcgi_param HTTP_PROXY "";
    }}
  }}
}}
""")
            start_process(self.stack, [NGINX, "-p", str(self.tmp), "-c", str(conf)], self.tmp / "nginx.log")
            eventual(lambda: ready(ref))
            ports.append(ref)
        boundary = "fixture-boundary"
        content = b"sample upload\x00"
        multi = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"fixture\"; filename=\"fixture.bin\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        payload = b"post payload" * 8000
        matrix = [
            ("/", "GET", None, {}, 200),
            ("/?mode=request", "POST", payload, {"Content-Type": "application/octet-stream"}, 200),
            ("/", "POST", multi, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 200),
            ("/?mode=error", "GET", None, {}, 500),
            ("/?mode=redirect", "GET", None, {}, 307),
            ("/?mode=cookies", "GET", None, {}, 200),
            ("/?mode=large", "GET", None, {}, 200),
        ]
        for uri, method, body, headers, expected in matrix:
            results = [call(port, uri, method, body, headers) for port in ports]
            for status, _, _ in results:
                self.assertEqual(status, expected)
            if len(results) == 2:
                self.assertEqual(results[0][2], results[1][2], uri)
            if "mode=large" in uri:
                self.assertEqual(results[0][2], b"x" * 1048576)
            elif "redirect" in uri:
                self.assertEqual(dict((k.lower(), v) for k, v in results[0][1])["location"], "/next")
            elif method == "POST" and body == multi:
                self.assertEqual(json.loads(results[0][2])["upload"]["sha256"], hashlib.sha256(content).hexdigest())
            elif method == "POST":
                self.assertEqual(json.loads(results[0][2])["bytes"], len(payload))
        self.assertGreaterEqual(self.metric("upstream_reused_total"), 5)
        print(f"PHP-FPM cases {len(matrix)}, NGINX differential {len(ports) == 2}", flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
