#!/usr/bin/env python3
"""Verify pre-transmission failover and half-open admission over real sockets."""
import http.server
import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from content_handlers import request, settings, wait
from integration import BIN, port


class Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.server.calls += 1
        status = self.server.status
        if self.server.delay:
            self.server.entered.set()
            self.server.release.wait(3)
        self.send_response(status)
        self.send_header("Content-Length", "1")
        self.end_headers()
        self.wfile.write(b"x")


class Resilience(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        self.origin.calls, self.origin.status, self.origin.delay = 0, 200, False
        self.origin.entered, self.origin.release = threading.Event(), threading.Event()
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        self.addCleanup(self.origin.server_close)
        self.addCleanup(self.origin.shutdown)
        self.listen = port()
        self.config = settings()
        self.config.update(listen=f"127.0.0.1:{self.listen}", admin=f"127.0.0.1:{port()}")
        self.config["routes"] = [dict(id="r", host="localhost", path_prefix="/", upstreams=["python"])]
        self.config["upstreams"] = self.config["upstreams"][:1]
        self.config["upstreams"][0].update(address=f"127.0.0.1:{self.origin.server_port}", failure_backoff_ms=120)

    def start(self):
        config = self.directory / "cauce.yaml"
        log = self.directory / "proxy.log"
        config.write_text(json.dumps({"version": 1, "proxy": self.config}))
        with log.open("w") as stderr:
            process = subprocess.Popen([BIN, config], cwd=self.directory, stderr=stderr)
        self.addCleanup(process.wait, 8)
        self.addCleanup(process.terminate)
        self.addCleanup(self.origin.release.set)
        wait(log, '"started"', process)

    def test_only_empty_safe_request_retries_and_budget_limits_consecutive_failures(self):
        backend = self.config["upstreams"][0]
        dead = dict(backend, id="dead", weight=64, address=f"127.0.0.1:{port()}",
                    resilience={"failures": 100, "max_backoff_ms": 300})
        self.config["upstreams"].append(dead)
        self.config["routes"][0].update(upstreams=["dead", "python"], balance="weighted_round_robin",
                                        connect_retry={"rate": 1, "concurrent": 1})
        self.start()
        self.assertEqual(request(self.listen, "/")[0], 200)
        self.assertEqual(request(self.listen, "/")[0], 502)
        self.assertEqual(request(self.listen, "/", method="POST")[0], 502)
        self.assertEqual(self.origin.calls, 1)

    def test_half_open_load_is_single_and_success_recovers(self):
        self.config["upstreams"][0]["resilience"] = {
            "failures": 1, "max_backoff_ms": 600, "eject_on_gateway_status": True,
        }
        self.origin.status = 503
        self.start()
        self.assertEqual(request(self.listen, "/")[2], b"x")
        self.assertEqual(request(self.listen, "/")[0], 503)
        self.assertEqual(self.origin.calls, 1)
        time.sleep(0.14)
        self.origin.status, self.origin.delay = 200, True
        with ThreadPoolExecutor(max_workers=10) as clients:
            leader = clients.submit(request, self.listen, "/")
            try:
                self.assertTrue(self.origin.entered.wait(2))
                results = list(clients.map(lambda _: request(self.listen, "/")[0], range(8)))
                self.assertEqual(results, [503] * 8)
                self.assertEqual(self.origin.calls, 2)
            finally:
                self.origin.release.set()
            self.assertEqual(leader.result()[0], 200)
        self.assertEqual(request(self.listen, "/")[0], 200)
        self.assertEqual(self.origin.calls, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
