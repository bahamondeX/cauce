#!/usr/bin/env python3
"""Real HTTP-01 validation and renewal against a local Pebble instance."""
import asyncio
import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

from content_handlers import settings, wait
from dns_upstream import Dns
from integration import BIN, port
from stream_tls import stop
from tls_gateway import issue, openssl

PEBBLE = os.environ.get("PEBBLE_BIN")
HOST = "app.cauce.test"


def poll(operation, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := operation():
            return result
        time.sleep(0.1)
    raise AssertionError("fixture did not reach the expected state")


def tls_get(connection, name=HOST):
    connection.sendall(f"GET / HTTP/1.1\r\nHost: {name}\r\n\r\n".encode())
    response = http.client.HTTPResponse(connection)
    response.begin()
    return response.status, response.read()


@unittest.skipUnless(PEBBLE, "set PEBBLE_BIN to a locally built v2.9.0")
class Pebble(unittest.TestCase):
    def test_issue_renew_preserve_session_and_restore_private_state(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as resources:
            directory = Path(temp)
            openssl(directory, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=fixture-ca", "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
                    "-keyout", "fixture-ca-key.pem", "-out", "fixture-ca.pem")
            issue(directory, 1, "pebble", "fixture-ca", "serverAuth", "localhost")
            issue(directory, 2, "bootstrap", "fixture-ca", "serverAuth", "bootstrap.test")
            ca_port, management, clear, secure, admin = [port() for _ in range(5)]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as check:
                check.bind(("127.0.0.1", 0))
                quic = check.getsockname()[1]
            config = directory / "pebble.json"
            config.write_text(json.dumps({"pebble": {
                "listenAddress": f"127.0.0.1:{ca_port}",
                "managementListenAddress": f"127.0.0.1:{management}",
                "certificate": str(directory / "pebble.pem"),
                "privateKey": str(directory / "pebble-key.pem"),
                "httpPort": clear, "tlsPort": secure,
                "keyAlgorithm": "ecdsa",
                "profiles": {"default": {"description": "local", "validityPeriod": 22}},
                "retryAfter": {"authz": 1, "order": 1},
            }}))
            dns = resources.enter_context(socketserver.UDPServer(("127.0.0.1", 0), Dns))
            dns.family = socket.AF_INET
            threading.Thread(target=dns.serve_forever, daemon=True).start()
            resources.callback(dns.shutdown)
            log = directory / "pebble.log"
            with log.open("wb") as output:
                process = subprocess.Popen([PEBBLE, "-config", config, "-dnsserver",
                                            f"127.0.0.1:{dns.server_address[1]}"],
                                           stdout=output, stderr=output, cwd=directory, env={
                    **os.environ, "PEBBLE_VA_NOSLEEP": "1", "PEBBLE_WFE_NONCEREJECT": "0",
                })
            resources.callback(stop, process)
            trust = ssl.create_default_context(cafile=directory / "fixture-ca.pem")
            def get_root():
                try:
                    with urllib.request.urlopen(f"https://localhost:{management}/roots/0", context=trust, timeout=2) as response:
                        return response.read()
                except OSError:
                    return None
            root = poll(get_root, timeout=5)
            (directory / "root.pem").write_bytes(root)

            runtime = settings()
            runtime.pop("listen")
            runtime["limits"]["client_headers_ms"] = 30_000
            runtime["limits"]["stream_idle_ms"] = 30_000
            runtime.update(admin=f"127.0.0.1:{admin}", upstreams=[], routes=[
                {"id":"site", "host":HOST, "path_prefix":"/", "content":{"type":"direct", "body":"ok", "status":200}},
                {"id":"extra", "host":"extra.cauce.test", "path_prefix":"/", "content":{"type":"direct", "body":"ok", "status":200}},
                {"id":"overlap", "host":"overlap.cauce.test", "path_prefix":"/", "content":{"type":"direct", "body":"ok", "status":200}},
            ])
            tls = {"cert_file": "bootstrap.pem", "key_file": "bootstrap-key.pem"}
            runtime["listeners"] = [{"listen": f"127.0.0.1:{clear}"}, {"listen": f"127.0.0.1:{secure}", "tls": tls}]
            runtime["http3"] = {"listen": f"127.0.0.1:{quic}", "tls": tls}
            state = directory / "acme"
            runtime["acme"] = {
                "directory": f"https://localhost:{ca_port}/dir", "directory_ca_file": str(directory / "fixture-ca.pem"),
                "state_dir": str(state), "accept_terms": True,
                "certificates": [[HOST, "extra.cauce.test"], [HOST, "overlap.cauce.test"]],
                "renew_before_seconds": 5, "check_interval_seconds": 1,
            }
            cauce_config = directory / "cauce.yaml"
            cauce_config.write_text(json.dumps({"version": 1, "proxy": runtime}))
            subprocess.run([BIN, "check", cauce_config], check=True, cwd=directory)
            self.assertFalse(state.exists())

            def start():
                cauce_log = directory / f"cauce-{time.monotonic_ns()}.log"
                with cauce_log.open("wb") as output:
                    proxy = subprocess.Popen([BIN, cauce_config], cwd=directory, stderr=output)
                resources.callback(stop, proxy)
                wait(cauce_log, '"started"', proxy)
                return proxy, cauce_log
            proxy, cauce_log = start()
            tls_client = ssl.create_default_context(cafile=directory / "root.pem")
            def dial(name=HOST):
                with contextlib.ExitStack() as close:
                    raw = close.enter_context(socket.create_connection(("127.0.0.1", secure), timeout=2))
                    connection = tls_client.wrap_socket(raw, server_hostname=name)
                    return connection
            def ready(name=HOST):
                try:
                    return dial(name)
                except (ssl.SSLError, OSError):
                    return None

            try:
                old = resources.enter_context(poll(ready))
            except AssertionError:
                self.fail(cauce_log.read_text() + "\n" + log.read_text())
            self.assertEqual(tls_get(old), (200, b"ok"))
            initial = old.getpeercert(binary_form=True)
            with dial("extra.cauce.test") as second_name:
                self.assertEqual(initial, second_name.getpeercert(binary_form=True))
                self.assertEqual(tls_get(second_name, "extra.cauce.test"), (200, b"ok"))
            with poll(lambda: ready("overlap.cauce.test")) as overlap:
                self.assertNotEqual(initial, overlap.getpeercert(binary_form=True))
                self.assertEqual(tls_get(overlap, "overlap.cauce.test"), (200, b"ok"))
                self.assertIn(("DNS", HOST), overlap.getpeercert()["subjectAltName"])
            with dial() as preferred:
                self.assertEqual(initial, preferred.getpeercert(binary_form=True))
            account = state / "account.json"
            certificate = state / "certificate-0.json"
            overlapping_certificate = state / "certificate-1.json"
            account_contents = account.read_bytes()
            self.assertEqual([path.stat().st_mode & 0o777 for path in [state, account, certificate, overlapping_certificate]],
                             [0o700, 0o600, 0o600, 0o600])

            def rotated():
                with ready() or contextlib.nullcontext() as connection:
                    if connection:
                        der = connection.getpeercert(binary_form=True)
                        if der != initial:
                            return der
            rotated_der = poll(rotated, timeout=25)
            self.assertEqual(tls_get(old), (200, b"ok"))
            self.assertGreaterEqual(cauce_log.read_text().count("acme_certificate_published"), 3)
            if os.environ.get("CAUCE_TEST_ACME_QUIC"):
                asyncio.run(verify_quic(quic, directory / "root.pem"))
            stop(proxy)
            _, restored_log = start()
            with dial() as restored:
                self.assertEqual(restored.getpeercert(binary_form=True), rotated_der)
            self.assertEqual(account.read_bytes(), account_contents)

            stop(process)
            poll(lambda: "acme_renewal_failed" in restored_log.read_text(), timeout=23)
            with dial() as available:
                self.assertEqual(available.getpeercert(binary_form=True), rotated_der)
                self.assertEqual(tls_get(available), (200, b"ok"))


async def verify_quic(port, ca):
    from aioquic.asyncio import connect
    from aioquic.h3.connection import H3_ALPN
    from aioquic.quic.configuration import QuicConfiguration

    configuration = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, server_name=HOST, cafile=str(ca))
    async with asyncio.timeout(4):
        async with connect("127.0.0.1", port, configuration=configuration) as client:
            await client.ping()
    print("QUIC handshake used the active ACME certificate and trusted the Pebble CA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
