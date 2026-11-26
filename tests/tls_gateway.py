#!/usr/bin/env python3
"""Exercise named certificates, TLS policy and mTLS on both socket directions."""
import http.server
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

from integration import BIN, ROOT, port


def openssl(directory, *args):
    return subprocess.run(
        ["openssl", *args], cwd=directory, check=True, capture_output=True,
    )


def issue(directory, serial, name, ca, purpose, dns=None):
    openssl(directory, "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-subj", f"/CN={name}", "-keyout", f"{name}-key.pem", "-out", f"{name}.csr")
    extension = directory / f"{name}.ext"
    extension.write_text(
        f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        f"extendedKeyUsage={purpose}\n" + (f"subjectAltName=DNS:{dns}\n" if dns else "")
    )
    openssl(directory, "x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.pem",
            "-CAkey", f"{ca}-key.pem", "-set_serial", str(serial), "-days", "2",
            "-sha256", "-extfile", extension.name, "-out", f"{name}.pem")


class Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        client = self.connection.getpeercert()
        name = dict(item for group in client["subject"] for item in group)["commonName"]
        body = name.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class TlsGatewayTests(unittest.TestCase):
    def test_sni_rotation_mutual_tls_and_generation_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for ca in ("trusted", "other"):
                openssl(directory, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "2", "-subj", f"/CN={ca}", "-keyout", f"{ca}-key.pem",
                        "-out", f"{ca}.pem")
            issue(directory, 1, "default", "trusted", "serverAuth", "localhost")
            issue(directory, 2, "alternate", "trusted", "serverAuth", "api.test")
            issue(directory, 3, "alternate-next", "trusted", "serverAuth", "api.test")
            issue(directory, 4, "backend", "trusted", "serverAuth", "backend.test")
            issue(directory, 5, "downstream", "trusted", "clientAuth")
            issue(directory, 6, "upstream-client", "trusted", "clientAuth")
            issue(directory, 7, "other-client", "other", "clientAuth")
            origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
            upstream_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            upstream_tls.load_cert_chain(directory / "backend.pem", directory / "backend-key.pem")
            upstream_tls.load_verify_locations(directory / "trusted.pem")
            upstream_tls.verify_mode = ssl.CERT_REQUIRED
            upstream_tls.minimum_version = ssl.TLSVersion.TLSv1_3
            origin.socket = upstream_tls.wrap_socket(origin.socket, server_side=True)
            origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
            origin_thread.start()
            try:
                self.exercise(directory, origin.server_port)
            finally:
                origin.shutdown()
                origin.server_close()
                origin_thread.join(timeout=2)

    def exercise(self, directory, upstream):
        listen, admin = port(), port()
        config = (
            (ROOT / "examples/gateway-mtls.yaml").read_text()
            .replace("127.0.0.1:8080", f"127.0.0.1:{listen}")
            .replace("localhost:8080", f"localhost:{listen}")
            .replace("127.0.0.1:9090", f"127.0.0.1:{admin}")
            .replace("127.0.0.1:8000", f"127.0.0.1:{upstream}")
            .replace("upstreams: [python, node]", "upstreams: [python]")
            .replace("api.example.test", "Api.Test")
            .replace("api-key.pem", "alternate-key.pem").replace("api.pem", "alternate.pem")
            .replace("downstream-ca.pem", "trusted.pem").replace("upstream-ca.pem", "trusted.pem")
            .replace("versions: [tls1.2, tls1.3]", "versions: [tls1.2]")
        )
        config_path = directory / "proxy.yaml"
        config_path.write_text(config)
        log = directory / "proxy.log"
        with log.open("w") as stderr:
            process = subprocess.Popen(
                [BIN, config_path], cwd=directory,
                env={**os.environ, "TOKIO_WORKER_THREADS": "2"}, stderr=stderr,
            )
        try:
            self.await_log(log, '"started"')
            good = self.client_context(directory, "downstream")
            _, status, body, first_session, _ = self.request(good, listen, "localhost")
            self.assertEqual((status, body), (200, b"upstream-client"))
            self.assertEqual(first_session.id != b"", True)
            self.assertTrue(self.request(good, listen, "localhost", first_session)[4])

            alternate, status, _, _, _ = self.request(good, listen, "api.test")
            self.assertEqual(status, 200)
            self.assertEqual(alternate, self.der(directory, "alternate"))
            fallback = self.client_context(directory, "downstream", check_hostname=False)
            for sn in ("missing.test", None):
                self.assertEqual(self.request(fallback, listen, sn)[0], self.der(directory, "default"))
            with self.assertRaises(ssl.SSLError):
                self.request(good, listen, "missing.test")
            for cert in (None, "other-client"):
                with self.assertRaises((OSError, EOFError)):
                    self.request(self.client_context(directory, cert), listen, "localhost")
            tls13 = self.client_context(directory, "downstream")
            tls13.minimum_version = ssl.TLSVersion.TLSv1_3
            with self.assertRaises(ssl.SSLError):
                self.request(tls13, listen, "localhost")

            rotated = config.replace("alternate.pem", "alternate-next.pem")
            invalid = rotated.replace("alternate-key.pem", "default-key.pem")
            config_path.write_text(invalid)
            process.send_signal(signal.SIGHUP)
            self.await_log(log, '"reload_rejected"')
            self.assertEqual(self.request(good, listen, "api.test")[0], alternate)

            rotated = rotated.replace("alternate-key.pem", "alternate-next-key.pem")
            rotated = rotated.replace("session_resumption: true", "session_resumption: false")
            config_path.write_text(rotated)
            process.send_signal(signal.SIGHUP)
            self.await_log(log, '"config_version":2')
            self.assertEqual(self.request(good, listen, "api.test")[0], self.der(directory, "alternate-next"))
            self.assertFalse(self.request(good, listen, "localhost", first_session)[4])

            disjoint = rotated.replace("client_ca_file: trusted.pem", "client_ca_file: other.pem")
            config_path.write_text(disjoint)
            process.send_signal(signal.SIGHUP)
            self.await_log(log, '"config_version":3')
            with self.assertRaises(ssl.SSLError):
                self.request(good, listen, "localhost", first_session)
            other = self.client_context(directory, "other-client")
            self.assertEqual(self.request(other, listen, "localhost")[1:3], (200, b"upstream-client"))

            untrusted_upstream = disjoint.replace(
                "cert_file: upstream-client.pem", "cert_file: other-client.pem",
            ).replace("key_file: upstream-client-key.pem", "key_file: other-client-key.pem")
            config_path.write_text(untrusted_upstream)
            process.send_signal(signal.SIGHUP)
            self.await_log(log, '"config_version":4')
            self.assertEqual(self.request(other, listen, "localhost")[1], 502)
        finally:
            process.terminate()
            self.assertEqual(process.wait(timeout=8), 0, log.read_text())

    @staticmethod
    def client_context(directory, client, check_hostname=True):
        context = ssl.create_default_context(cafile=directory / "trusted.pem")
        context.check_hostname = check_hostname
        if client:
            context.load_cert_chain(directory / f"{client}.pem", directory / f"{client}-key.pem")
        return context

    @staticmethod
    def request(context, listen, name, session=None):
        import http.client

        with socket.create_connection(("127.0.0.1", listen), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname=name, session=session) as transport:
                transport.sendall(
                    f"GET / HTTP/1.1\r\nHost: localhost:{listen}\r\nConnection: close\r\n\r\n".encode()
                )
                response = http.client.HTTPResponse(transport)
                response.begin()
                body = response.read()
                return transport.getpeercert(binary_form=True), response.status, body, transport.session, transport.session_reused

    @staticmethod
    def der(directory, name):
        return ssl.PEM_cert_to_DER_cert((directory / f"{name}.pem").read_text())

    def await_log(self, log, token):
        deadline = time.monotonic() + 7
        while token not in log.read_text():
            if time.monotonic() >= deadline:
                self.fail(f"{token} missing in {log.read_text()}")
            time.sleep(0.03)


if __name__ == "__main__":
    unittest.main()
