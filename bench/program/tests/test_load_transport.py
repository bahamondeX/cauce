"""Real-socket checks of the generator's transport and offer identities."""

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import unittest

PROGRAM = Path(__file__).resolve().parents[1]
LOAD = Path(os.environ.get("CAUCE_LOAD", PROGRAM / "load/target/debug/cauce-load")).resolve()
NODE = """
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import fs from 'node:fs';
const [protocol, key, cert] = process.argv.slice(1);
const tls = key === 'none' ? null : {key: fs.readFileSync(key), cert: fs.readFileSync(cert)};
const create = protocol === 'http1'
    ? (tls ? () => https.createServer(tls) : () => http.createServer())
    : (tls ? () => http2.createSecureServer(tls) : () => http2.createServer());
const server = create();
server.on('request', async (request, response) => {
    if (request.url === '/disconnect') {
        request.socket.destroy();
        return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    if (request.headers.accept !== '*/*' || request.method !== 'POST' ||
        !Buffer.concat(chunks).equals(Buffer.alloc(12, 'x'))) {
        response.writeHead(400);
        response.end();
        return;
    }
    response.setHeader('x-request-id', request.headers['x-cauce-bench-offer']);
    response.end(Buffer.alloc(1024, 'x'));
});
server.listen(0, '127.0.0.1', () => console.log(server.address().port));
"""


@unittest.skipUnless(LOAD.is_file() and shutil.which("node") and shutil.which("openssl"),
                     "build the debug generator and install Node and OpenSSL")
class LoadTransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.key = self.path / "key.pem"
        self.cert = self.path / "cert.pem"
        self.ca = self.path / "ca.pem"

    def certificate(self):
        ca_key = self.path / "ca.key"
        request = self.path / "leaf.csr"
        extensions = self.path / "leaf.ext"
        extensions.write_text("basicConstraints=critical,CA:FALSE\n"
                              "subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n")

        def openssl(*arguments):
            subprocess.run(["openssl", *arguments], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=10)

        openssl("req", "-new", "-newkey", "rsa:2048", "-x509", "-nodes",
                "-subj", "/CN=load fixture CA", "-days", "1",
                "-keyout", str(ca_key), "-out", str(self.ca))
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes",
                "-subj", "/CN=localhost", "-keyout", str(self.key), "-out", str(request))
        openssl("x509", "-req", "-in", str(request), "-CA", str(self.ca),
                "-CAkey", str(ca_key), "-CAcreateserial", "-days", "1", "-extfile",
                str(extensions), "-out", str(self.cert))

    def run_load(self, protocol, tls, host=None, route="/"):
        with subprocess.Popen(
            ["node", "--input-type=module", "-e", NODE, protocol,
             str(self.key) if tls else "none", str(self.cert)],
            stdout=subprocess.PIPE, text=True,
        ) as server:
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(server.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(5), "Node never advertised its fixture port")
                    port = int(server.stdout.readline())
                scheme = "https" if tls else "http"
                spec = {"url": f"{scheme}://{host or 'localhost'}:{port}{route}",
                        "protocol": protocol, "rate": 10, "seconds": 1, "warmup_seconds": 1,
                        "concurrency": 4, "queue": 16, "request_bytes": 12,
                        "response_bytes": 1024, "response_byte": 120, "trace_offers": True}
                if tls:
                    spec["ca_file"] = str(self.ca)
                (self.path / "spec").write_text(json.dumps(spec))
                subprocess.run([str(LOAD), "spec", "offers.jsonl", "summary.json"],
                               cwd=self.path, check=True, timeout=10)
                return [[json.loads(line) for line in (self.path / name).read_text().splitlines()]
                        for name in ("offers.warmup.jsonl", "offers.jsonl")]
            finally:
                server.terminate()

    def test_http1_http2_clear_and_tls_keep_offer_identity(self):
        self.certificate()
        for tls in (False, True):
            for protocol in ("http1", "http2"):
                with self.subTest(protocol=protocol, tls=tls):
                    warmup, measured = self.run_load(protocol, tls)
                    for rows, offset in ((warmup, 1_000_000_000), (measured, 0)):
                        self.assertEqual(len(rows), 10)
                        for n, row in enumerate(rows):
                            self.assertEqual(row["outcome"], "success", row)
                            self.assertEqual(row["request_id"], str(offset + n))

    def test_tls_rejects_the_wrong_name(self):
        self.certificate()
        for rows in self.run_load("http1", True, "127.0.0.1"):
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row["outcome"] == "transport_error" and row["status"] is None
                                for row in rows))

    def test_no_response_disconnect_is_accounted_as_transport_error(self):
        for rows in self.run_load("http1", False, route="/disconnect"):
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row["outcome"] == "transport_error" and row["status"] is None
                                for row in rows))


if __name__ == "__main__":
    unittest.main()
