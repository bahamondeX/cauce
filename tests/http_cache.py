#!/usr/bin/env python3
"""Cache correctness, request collapse, invalidation and failure over sockets."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import http.client
import http.server
import json
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from content_handlers import request, settings, wait
from integration import BIN, port


class Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, status, body=b"", **headers):
        try:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            if status != 304 and not headers.get("Transfer_Encoding") and not headers.get("Content_Length"):
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_POST(self):
        with self.server.lock:
            self.server.version += 1
        self.reply(204)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path
        with self.server.lock:
            self.server.calls[path] += 1
            count = self.server.calls[path]
            version = self.server.version
        rules = "public, max-age=60"
        body = f"response {version}".encode()
        if path == "/same":
            time.sleep(0.2)
        if path in ("/race", "/cancel") and count == 1:
            self.server.entered.set()
            self.server.release.wait(timeout=3)
        if path == "/color":
            self.reply(200, self.headers.get("x-color", "none").encode(), Cache_Control=rules, Vary="X-Color")
            return
        if path == "/unconfigured-vary":
            self.reply(200, body, Cache_Control=rules, Vary="Accept-Language")
            return
        if path == "/private":
            self.reply(200, body, Cache_Control="public, private, max-age=60")
            return
        if path == "/cookie":
            self.reply(200, body, Cache_Control=rules, Set_Cookie="session=private")
            return
        if path == "/replace" and self.headers.get("If-None-Match") == '"current"':
            self.reply(304, Cache_Control=rules, ETag='"current"')
            return
        if path in ("/replace", "/stale", "/swr"):
            rules = "public, max-age=1, stale-if-error=10, stale-while-revalidate=10"
            if path == "/stale" and count > 1:
                self.reply(503, b"origin unavailable")
                return
            if path == "/swr" and count == 2:
                self.server.entered.set()
                self.server.release.wait(timeout=3)
                body = b"updated"
        if path.startswith("/weighted/"):
            body = b"x" * 4096
        if path == "/broken":
            self.close_connection = True
            self.reply(200, b"incomplete", Cache_Control=rules, Content_Length="2048", Connection="close")
            return
        if path == "/large":
            data = b"x" * 10240
            self.reply(200, b"2800\r\n" + data + b"\r\n0\r\n\r\n", Cache_Control=rules, Transfer_Encoding="chunked")
            return
        if path == "/bad-304" and count > 1:
            self.reply(304, Cache_Control="private", ETag='"current"')
            return
        self.reply(200, body, Cache_Control=rules, ETag='"current"')


class CacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name).resolve()
        origin = self.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        origin.lock = threading.Lock()
        origin.calls = Counter()
        origin.entered = threading.Event()
        origin.release = threading.Event()
        origin.version = 1
        target = threading.Thread(target=origin.serve_forever, daemon=True)
        target.start()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        self.addCleanup(origin.release.set)
        self.listen = port()
        self.path = directory / "proxy.yaml"
        self.log = directory / "gateway.log"
        self.data = settings()
        self.data.update(listen=f"127.0.0.1:{self.listen}", admin=f"127.0.0.1:{port()}")
        self.data["upstreams"] = self.data["upstreams"][:1]
        self.data["upstreams"][0]["address"] = f"127.0.0.1:{origin.server_port}"
        self.data["routes"] = [dict(id="shared", host="*.example.test", path_prefix="/", upstreams=["python"],
            cache=dict(max_size="128KiB", max_entry_size="8KiB", max_entries=3, max_fills=4,
                       max_waiters=16, ttl="60s", stale_if_error="10s", stale_while_revalidate="10s",
                       vary=["x-color", "accept-encoding"], bypass_header="x-cache-bypass"))]
        self.write()
        with self.log.open("w") as errors:
            self.proxy = subprocess.Popen([BIN, self.path], cwd=directory, stderr=errors)
        self.addCleanup(self.stop)
        wait(self.log, '"started"', self.proxy)

    def write(self):
        new = self.path.with_suffix(".tmp")
        new.write_text(json.dumps(dict(version=1, proxy=self.data)))
        new.replace(self.path)

    def stop(self):
        self.origin.release.set()
        self.proxy.terminate()
        self.proxy.wait(timeout=8)

    def get(self, path, headers=None, host="a.example.test", method="GET"):
        return request(self.listen, path, method=method, headers=headers, host=host)

    def hit(self, result):
        self.assertIn("hit", result[1]["cache-status"])

    def test_coalesced_fill_and_distinct_request_identity(self):
        barrier = threading.Barrier(10)
        def query(_):
            barrier.wait(timeout=2)
            return self.get("/same")
        with ThreadPoolExecutor(max_workers=10) as workers:
            rows = list(workers.map(query, range(10)))
        self.assertEqual(self.origin.calls["/same"], 1)
        self.assertEqual(sum("hit" in row[1]["cache-status"] for row in rows), 9)
        self.assertEqual(len({row[1]["x-request-id"] for row in rows}), 10)
        self.assertEqual({row[2] for row in rows}, {b"response 1"})
        head = self.get("/same", method="HEAD")
        self.hit(head)
        self.assertEqual((head[1]["content-length"], head[2]), ("10", b""))
        result = self.get("/same", {"If-None-Match": '"current"'})
        self.assertEqual(result[0], 304)
        self.assertEqual(self.origin.calls["/same"], 1)
        self.assertIn("age", self.get("/same")[1])

    def test_isolation_and_conservative_shared_policy(self):
        self.assertEqual(self.get("/value", {"Cache-Control": "only-if-cached"})[0], 504)
        self.get("/value?a=1")
        self.hit(self.get("/value?a=1", host="A.example.test"))
        for path, host in (("/value?a=1","b.example.test"),("/value?a=2","a.example.test")):
            self.assertIn("fwd=uri-miss", self.get(path, host=host)[1]["cache-status"])
        for color in ("red", "blue", "red"):
            self.assertEqual(self.get("/color", {"X-Color": color})[2], color.encode())
        self.assertEqual(self.origin.calls["/color"], 2)
        for path in ("/private", "/cookie", "/unconfigured-vary"):
            self.get(path)
            self.get(path)
            self.assertEqual(self.origin.calls[path], 2)
        for headers in ({"Authorization":"Basic Zm9v"}, {"Cookie":"sid=1"}, {"X-Cache-Bypass":"1"},
                        {"Cache-Control":"no-store"}, {"Range":"bytes=0-4"}):
            with self.subTest(headers=headers):
                self.assertIn("fwd=bypass", self.get("/value", headers)[1]["cache-status"])
        self.assertEqual(self.origin.calls["/value"], 5)

    def test_revalidation_and_stale_policies(self):
        self.get("/replace")
        update = self.get("/replace", {"Cache-Control": "no-cache"})
        self.assertEqual(update[0], 200)
        self.assertIn("fwd-status=304", update[1]["cache-status"])
        self.hit(self.get("/replace"))
        self.assertEqual(self.origin.calls["/replace"], 2)
        self.get("/stale")
        self.get("/swr")
        time.sleep(1.12)
        row = self.get("/stale")
        self.assertEqual((row[0], row[2]), (200, b"response 1"))
        self.assertIn("fwd-status=503", row[1]["cache-status"])
        with ThreadPoolExecutor(max_workers=1) as worker:
            leader = worker.submit(self.get, "/swr")
            self.assertTrue(self.origin.entered.wait(timeout=2))
            follower = self.get("/swr")
            self.assertIn("ttl=0", follower[1]["cache-status"])
            self.assertEqual(follower[2], b"response 1")
            self.origin.release.set()
            self.assertEqual(leader.result()[2], b"updated")
        self.hit(self.get("/swr"))
        self.get("/bad-304")
        self.assertEqual(self.get("/bad-304", {"Cache-Control":"no-cache"})[0], 502)
        self.assertEqual(self.get("/bad-304", {"Cache-Control":"only-if-cached"})[0], 504)

    def test_overflow_truncation_and_eviction(self):
        for _ in range(2):
            self.assertEqual(len(self.get("/large")[2]), 10240)
            with self.assertRaises((http.client.HTTPException, ConnectionError)):
                self.get("/broken")
        self.assertEqual(self.origin.calls["/large"], 2)
        self.assertEqual(self.origin.calls["/broken"], 2)
        for i in range(4):
            self.get(f"/eviction?row={i}")
        self.assertEqual(self.get("/eviction?row=0", {"Cache-Control":"only-if-cached"})[0], 504)
        self.hit(self.get("/eviction?row=3"))

    def test_byte_quota_evicts_below_entry_count_limit(self):
        self.data["routes"][0]["cache"].update(max_size="16KiB", max_entry_size="4KiB", max_entries=64, max_fills=2)
        self.write()
        self.proxy.send_signal(signal.SIGHUP)
        wait(self.log, '"reloaded"', self.proxy)
        for number in range(4):
            self.assertEqual(len(self.get(f"/weighted/{number}")[2]), 4096)
        self.assertEqual(self.get("/weighted/0", {"Cache-Control":"only-if-cached"})[0], 504)
        self.hit(self.get("/weighted/3"))

    def test_cancelled_leader_releases_fill_ownership(self):
        with ThreadPoolExecutor(max_workers=1) as worker:
            with socket.create_connection(("127.0.0.1", self.listen), timeout=3) as leader:
                leader.sendall(b"GET /cancel HTTP/1.1\r\nHost: a.example.test\r\n\r\n")
                self.assertTrue(self.origin.entered.wait(timeout=2))
                waiter = worker.submit(self.get, "/cancel")
            self.origin.release.set()
            self.assertEqual(waiter.result(timeout=3)[2], b"response 1")
        self.hit(self.get("/cancel"))
        self.assertLessEqual(self.origin.calls["/cancel"], 2)

    def test_invalidation_prevents_stale_inflight_publication(self):
        with ThreadPoolExecutor(max_workers=1) as worker:
            old_reader = worker.submit(self.get, "/race")
            self.assertTrue(self.origin.entered.wait(timeout=2))
            self.assertEqual(self.get("/race", method="POST")[0], 204)
            self.origin.release.set()
            self.assertEqual(old_reader.result()[2], b"response 1")
        self.assertEqual(self.get("/race", {"Cache-Control":"only-if-cached"})[0], 504)
        self.assertEqual(self.get("/race")[2], b"response 2")
        self.hit(self.get("/race"))
        # Reload publishes a separate cache generation.
        self.proxy.send_signal(signal.SIGHUP)
        wait(self.log, '"reloaded"', self.proxy)
        self.assertEqual(self.get("/race", {"Cache-Control":"only-if-cached"})[0], 504)


if __name__ == "__main__":
    unittest.main()
