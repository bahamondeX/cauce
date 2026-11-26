#!/usr/bin/env python3
"""Disk-tier read, quota, recovery, and invalidation over real HTTP sockets."""
from collections import Counter
from pathlib import Path
import http.server
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from content_handlers import request, settings, wait
from http_cache import Origin
from integration import BIN, port


class DiskCache(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve()
        self.storage = self.path / "disk"
        self.config = self.path / "cauce.yaml"
        self.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        self.origin.calls, self.origin.lock = Counter(), threading.Lock()
        self.origin.version = 1
        threading.Thread(target=self.origin.serve_forever, daemon=True).start()
        self.addCleanup(self.origin.server_close)
        self.addCleanup(self.origin.shutdown)
        self.listen = port()
        config = settings()
        config.update(listen=f"127.0.0.1:{self.listen}", admin=f"127.0.0.1:{port()}")
        config["upstreams"] = config["upstreams"][:1]
        config["upstreams"][0]["address"] = f"127.0.0.1:{self.origin.server_port}"
        config["routes"] = [{"id":"disk", "host":"localhost", "path_prefix":"/", "upstreams":["python"],
                            "cache":{"max_size": "16KiB", "max_entry_size":"4KiB", "max_entries":64,
                                     "max_fills":2, "disk":{"directory":str(self.storage),"max_size":"32KiB"}}}]
        self.config.write_text(json.dumps({"version":1, "proxy":config}))
        self.start()

    def start(self):
        self.log = self.path / f"gateway-{time.monotonic_ns()}.log"
        with self.log.open("w") as stderr:
            self.proxy = subprocess.Popen([BIN, self.config], cwd=self.path, stderr=stderr)
        process = self.proxy
        def finish():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=8)
        self.addCleanup(finish)
        wait(self.log, '"started"', self.proxy)

    def stop(self, crash=False):
        self.proxy.send_signal(signal.SIGKILL if crash else signal.SIGTERM)
        self.proxy.wait(timeout=8)

    def get(self, url, cache_only=False, method="GET"):
        headers = {"Cache-Control":"only-if-cached"} if cache_only else {}
        return request(self.listen, url, method, headers)

    def objects(self):
        return list(self.storage.glob("*.cauce-object"))

    def await_disk(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            self.assertLess(time.monotonic(), deadline, self.log.read_text())
            time.sleep(0.025)

    def test_disk_miss_memory_eviction_and_clean_restart(self):
        for index in range(4):
            self.assertEqual(self.get(f"/weighted/{index}")[0], 200)
        self.await_disk(lambda: len(self.objects()) == 4)
        first = self.get("/weighted/0", cache_only=True)
        self.assertEqual(first[2], b"x" * 4096)
        self.assertEqual(self.origin.calls["/weighted/0"], 1)
        self.stop()
        self.assertTrue((self.storage / "clean").is_file())
        self.start()
        self.assertEqual(self.get("/weighted/0", cache_only=True, method="HEAD")[0], 200)
        self.assertEqual(self.get("/weighted/0")[2], b"x" * 4096)
        self.assertEqual(self.origin.calls["/weighted/0"], 1)
        self.assertEqual(sum(p.stat().st_size for p in self.objects()) <= 32 * 1024, True)

    def test_corrupt_entry_and_ungraceful_restart_are_safe(self):
        self.get("/default")
        self.await_disk(lambda: len(self.objects()) == 1)
        self.stop()
        with self.objects()[0].open("r+b") as damaged:
            damaged.seek(-1, os.SEEK_END)
            damaged.write(b"\x00\x01")
        self.start()
        self.assertEqual(self.get("/default", cache_only=True)[0], 504)
        self.get("/default")
        self.await_disk(lambda: len(self.objects()) == 1)
        self.stop(crash=True)
        self.start()
        self.assertEqual(self.get("/default", cache_only=True)[0], 504)
        self.assertEqual(self.objects(), [])

    def test_write_invalidates_disk_and_fifo_eviction_respects_quota(self):
        self.assertEqual(self.get("/mutation")[2], b"response 1")
        self.await_disk(lambda: len(self.objects()) == 1)
        self.assertEqual(self.get("/mutation", method="POST")[0], 204)
        self.assertEqual(self.get("/mutation", cache_only=True)[0], 504)
        self.await_disk(lambda: not self.objects())
        for index in range(12):
            self.assertEqual(self.get(f"/weighted/{index}")[0], 200)
            time.sleep(0.03)
        self.assertLessEqual(sum(p.stat().st_size for p in self.objects()), 32 * 1024)
        self.assertEqual(self.get("/weighted/0", cache_only=True)[0], 504)
        self.assertEqual(self.get("/weighted/11", cache_only=True)[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
