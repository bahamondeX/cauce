#!/usr/bin/env python3
"""Real sockets for the compiled local content path and filesystem policy."""
import email.utils
import gzip
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest

from integration import BIN, ROOT, port


def settings():
    import tomllib

    return tomllib.loads((ROOT / "examples/cauce.toml").read_text())


def request(listen, path, method="GET", headers=None, host="localhost"):
    connection = http.client.HTTPConnection("127.0.0.1", listen, timeout=3)
    try:
        connection.request(method, path, headers={"Host": host, **(headers or {})})
        response = connection.getresponse()
        return response.status, dict((name.lower(), value) for name, value in response.getheaders()), response.read()
    finally:
        connection.close()


def wait(path, token, process):
    deadline = time.monotonic() + 5
    while token not in path.read_text():
        if process.poll() is not None or time.monotonic() >= deadline:
            raise AssertionError(path.read_text())
        time.sleep(0.02)


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.public = self.directory / "public"
        self.public.mkdir()
        (self.public / "docs").mkdir()
        (self.public / "docs" / "index.html").write_text("document")
        (self.public / "listing").mkdir()
        (self.public / "listing" / "<visible>.txt").write_text("visible")
        (self.public / "listing" / ".private").write_text("hidden")
        (self.public / "sample.txt").write_bytes(b"0123456789")
        (self.public / "large.txt").write_bytes(b"content\n" * 18000)
        (self.public / "client.KEY").write_text("private material")
        (self.directory / "escape.txt").write_text("secret")
        (self.public / "escape.txt").symlink_to(self.directory / "escape.txt")
        (self.public / "escape-directory").symlink_to(self.directory, target_is_directory=True)
        os.mkfifo(self.public / "fifo")
        now = int(time.time()) - 20
        os.utime(self.public / "sample.txt", (now, now))
        self.listen = port()
        self.conf = self.directory / "gateway.yaml"
        self.log = self.directory / "gateway.log"
        self.data = settings()
        self.data.update(listen=f"127.0.0.1:{self.listen}", admin=f"127.0.0.1:{port()}",
                         upstreams=[], routes=[
            dict(id="static", host="localhost", path_prefix="/",
                 content={"type": "static", "root": str(self.public), "listing": True,
                          "cache_control": "public, max-age=120", "expires_seconds": 120}),
            dict(id="alias", host="localhost", path_prefix="/files",
                 content={"type": "static", "alias": str(self.public)}),
            dict(id="spa", host="localhost", path_prefix="/spa",
                 content={"type": "static", "alias": str(self.public),
                          "try_files": ["$uri", "$uri/", "sample.txt"]}),
            dict(id="direct", host="localhost", path_prefix="/direct",
                 content={"type": "direct", "status": 201, "body": "direct"}),
        ])
        self.write()
        with self.log.open("w") as errors:
            self.process = subprocess.Popen([BIN, self.conf], cwd=self.directory, stderr=errors)
        self.addCleanup(self.stop)
        wait(self.log, '"started"', self.process)

    def write(self):
        replacement = self.conf.with_suffix(".tmp")
        replacement.write_text(json.dumps({"version": 1, "proxy": self.data}))
        replacement.replace(self.conf)

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=8)

    def query(self, path, method="GET", headers=None):
        return request(self.listen, path, method, headers)

    def test_metadata_range_and_if_range(self):
        status, headers, body = self.query("/sample.txt")
        self.assertEqual((status, body), (200, b"0123456789"))
        self.assertEqual(headers["content-type"], "text/plain")
        self.assertEqual(headers["content-length"], "10")
        self.assertEqual(headers["cache-control"], "public, max-age=120")
        self.assertIn("expires", headers)
        self.assertIn("x-request-id", headers)
        self.assertEqual(self.query("/sample.txt", "HEAD")[2], b"")
        self.assertEqual(self.query("/sample.txt", headers={"If-None-Match": headers["etag"]})[0], 304)
        self.assertEqual(self.query("/sample.txt", headers={"If-Modified-Since": headers["last-modified"]})[0], 304)
        self.assertEqual(self.query("/sample.txt", headers={"If-Match": '"different"'})[0], 412)
        status, partial, body = self.query("/sample.txt", headers={"Range": "bytes=2-4"})
        self.assertEqual((status, body, partial["content-range"]), (206, b"234", "bytes 2-4/10"))
        self.assertEqual(self.query("/sample.txt", headers={"Range": "bytes=-3"})[2], b"789")
        status, partial, body = self.query("/sample.txt", headers={"Range": "bytes=0-1,8-9"})
        self.assertEqual(status, 206)
        self.assertTrue(partial["content-type"].startswith("multipart/byteranges; boundary="))
        self.assertEqual(len(body), int(partial["content-length"]))
        self.assertIn(b"Content-Range: bytes 0-1/10\r\n\r\n01", body)
        self.assertIn(b"Content-Range: bytes 8-9/10\r\n\r\n89", body)
        self.assertEqual(self.query("/sample.txt", headers={"Range": "bytes=100-102"})[0], 416)
        self.assertEqual(self.query("/sample.txt", headers={"Range": "bytes=2-", "If-Range": headers["etag"]})[0], 200)
        self.assertEqual(self.query("/sample.txt", headers={"Range": "bytes=2-", "If-Range": headers["last-modified"]})[0], 206)
        future = email.utils.formatdate(time.time() + 60, usegmt=True)
        self.assertEqual(self.query("/sample.txt", headers={"Range": "bytes=2-", "If-Range": future})[0], 200)
        self.assertEqual(self.query("/large.txt")[2], b"content\n" * 18000)

    def test_compression_negotiates_both_codecs_and_preserves_ranges(self):
        import brotli

        self.data["routes"][0]["compression"] = {"algorithms": ["br", "gzip"], "min_size": 1}
        self.data["routes"][0]["content"]["precompressed"] = True
        contents = (self.public / "large.txt").read_bytes()
        precompressed = gzip.compress(contents, mtime=12345)
        (self.public / "large.txt.gz").write_bytes(precompressed)
        self.write()
        self.process.send_signal(signal.SIGHUP)
        wait(self.log, '"reloaded"', self.process)
        self.assertEqual(self.query("/large.txt")[2], contents)
        status, headers, body = self.query("/large.txt", headers={"Accept-Encoding": "gzip;q=1, br;q=0.5"})
        self.assertEqual((status, headers["content-encoding"], body), (200, "gzip", precompressed))
        self.assertEqual(int(headers["content-length"]), len(precompressed))
        self.assertIn("accept-encoding", headers["vary"].lower())
        status, headers, body = self.query("/large.txt", headers={"Accept-Encoding": "gzip;q=0.5, br;q=1"})
        self.assertEqual((status, headers["content-encoding"], brotli.decompress(body)), (200, "br", contents))
        self.assertNotIn("content-length", headers)
        self.assertTrue(headers["etag"].startswith("W/"))
        status, headers, body = self.query("/large.txt", headers={"Accept-Encoding": "gzip", "Range": "bytes=0-2"})
        self.assertEqual((status, body), (206, contents[:3]))
        self.assertNotIn("content-encoding", headers)
        self.assertNotIn("content-encoding", self.query("/large.txt", headers={"Cookie":"session=secret","Accept-Encoding":"gzip"})[1])
        self.assertEqual(self.query("/large.txt", headers={"Accept-Encoding": "*;q=0"})[0], 406)

    def test_slow_reader_holds_one_descriptor_and_recovers_on_disconnect(self):
        self.data["routes"][1]["content"]["max_open"] = 1
        with (self.public / "held.txt").open("wb") as file:
            file.truncate(32 * 1024 * 1024)
        self.write()
        self.process.send_signal(signal.SIGHUP)
        wait(self.log, '"reloaded"', self.process)
        with socket.socket() as peer:
            peer.settimeout(3)
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            peer.connect(("127.0.0.1", self.listen))
            peer.sendall(b"GET /files/held.txt HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertIn(b"200 OK", peer.recv(4096))
            self.assertEqual(self.query("/files/sample.txt")[0], 503)
        until = time.monotonic() + 4
        while self.query("/files/sample.txt")[0] != 200:
            self.assertLess(time.monotonic(), until)
            time.sleep(.025)

    def test_access_alias_listing_and_rollback(self):
        self.assertEqual(self.query("/files/sample.txt")[2], b"0123456789")
        self.assertEqual(self.query("/spa/absent")[2], b"0123456789")
        self.assertEqual(self.query("/docs/?page=1")[2], b"document")
        self.assertEqual(self.query("/docs?page=1")[1]["location"], "/docs/?page=1")
        self.assertEqual(self.query("/direct")[0::2], (201, b"direct"))
        self.assertEqual(self.query("/direct", "HEAD")[1]["content-length"], "6")
        self.assertEqual(self.query("/sample.txt", "POST")[0], 405)
        listing = self.query("/listing/")[2]
        self.assertIn(b"%3Cvisible%3E.txt", listing)
        self.assertIn(b"&lt;visible&gt;.txt", listing)
        self.assertNotIn(b".private", listing)
        for path in ("/escape.txt", "/escape-directory/escape.txt", "/fifo", "/client.KEY", "/listing/.private"):
            self.assertEqual(self.query(path)[0], 404, path)
        for path in ("/%2e%2e/escape.txt", "/files/%252e%252e/escape.txt", "/files/sample.txt%00", "/files//sample.txt"):
            self.assertEqual(self.query(path)[0], 400, path)
        root_link = self.directory / "root-link"
        root_link.symlink_to(self.public)
        self.data["routes"][0]["content"]["root"] = str(root_link)
        self.write()
        self.process.send_signal(signal.SIGHUP)
        wait(self.log, '"reload_rejected"', self.process)
        self.assertEqual(self.query("/sample.txt")[0], 200)
        new = self.directory / "new"
        new.mkdir()
        (new / "sample.txt").write_text("new generation")
        self.data["routes"][0]["content"]["root"] = str(new)
        self.write()
        self.process.send_signal(signal.SIGHUP)
        wait(self.log, '"reloaded"', self.process)
        self.assertEqual(self.query("/sample.txt")[2], b"new generation")


if __name__ == "__main__":
    unittest.main()
