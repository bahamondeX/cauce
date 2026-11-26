#!/usr/bin/env python3
"""Socket security gates and Postfix/Dovecot interoperability."""
import base64
import contextlib
import http.server
import imaplib
import json
import os
from pathlib import Path
import poplib
import signal
import socket
import smtplib
import socketserver
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from dns_upstream import Dns
sys.path.insert(0, str(ROOT / "tests/stream"))
from interop import CAUCE, TOKEN, ThreadTcp, eventual, free_port, start_process, start_server, tcp

USER = "fixture"
PASSWORD = "test-only-password"
MAILD = {"smtp": 25, "imap": 143, "pop3": 110}
NGINX_MAIL = os.environ.get("NGINX_MAIL")


class Auth(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        protocol = self.headers["Auth-Protocol"]
        self.server.requests.append({
            "protocol": protocol, "ssl": self.headers["Auth-SSL"],
            "method": self.headers["Auth-Method"], "peer": self.headers["Client-IP"],
            "attempt": int(self.headers["Auth-Login-Attempt"]),
        })
        if self.server.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/next")
        else:
            self.send_response(200)
            valid = (unquote(self.headers["Auth-User"]), unquote(self.headers["Auth-Pass"])) == (USER, PASSWORD)
            valid |= self.server.mode == "approve"
            if valid:
                port = self.server.ports[protocol] if self.server.mode != "outside" else 1
                self.send_header("Auth-Status", "OK")
                if self.server.mode == "duplicate":
                    self.send_header("Auth-Status", "OK")
                self.send_header("Auth-Server", "127.0.0.1")
                self.send_header("Auth-Port", str(port))
            else:
                self.send_header("Auth-Status", "opaque backend text")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_):
        pass


class MailTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="c8-mail-")))
        self.auth = start_server(self.stack, http.server.ThreadingHTTPServer, Auth)
        self.auth.requests, self.auth.mode = [], "reject_bad"
        self.auth.ports = {protocol: free_port() for protocol in MAILD}
        self.ports = {protocol: free_port() for protocol in MAILD}
        self.admin = free_port()
        self.cert = self.tmp / "cert.pem"
        self.key = self.tmp / "key.pem"
        req = self.tmp / "req.conf"
        req.write_text("""[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=localhost
[ext]
subjectAltName=DNS:localhost,IP:127.0.0.1
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
""")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-config", str(req), "-keyout", str(self.key), "-out", str(self.cert)],
                       check=True, capture_output=True)
        self.tls = ssl.create_default_context(cafile=str(self.cert))

    def configuration(self, mode="starttls", **options):
        config = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
        for entry in ("listen", "routes", "upstreams"):
            config.pop(entry)
        config["logs"]["enabled"] = False
        config["admin"] = f"127.0.0.1:{self.admin}"
        config["mail"] = [
            {"listen": f"127.0.0.1:{port}", "protocol": proto, "tls_mode": mode,
             "tls": {"cert_file": str(self.cert), "key_file": str(self.key)},
             "auth_http": f"http://127.0.0.1:{self.auth.server_port}/auth",
             "auth_mechanisms": {"smtp": ["plain", "login"], "imap": ["login", "plain"],
                                 "pop3": ["user_pass"]}[proto],
             "backends": [{"address": f"127.0.0.1:{self.auth.ports[proto]}"}], **options}
            for proto, port in self.ports.items()
        ]
        return {"version": 1, "proxy": config}

    def start(self, mode="starttls", configuration=None, config_file="cauce", **options):
        self.config = self.tmp / f"{config_file}.yaml"
        self.log = self.tmp / f"{config_file}.log"
        self.config.write_text(json.dumps(configuration or self.configuration(mode, **options)))
        proc = start_process(self.stack, [CAUCE, str(self.config)], self.log,
                             env={**os.environ, "CAUCE_ADMIN_TOKEN": TOKEN})
        try:
            eventual(lambda: '"event":"started"' in self.log.read_text())
        except AssertionError:
            self.fail(self.log.read_text())
        return proc

    def origin(self):
        command = ["docker", "run", "--rm", "-d", "--pull=never"]
        for proto, port in MAILD.items():
            command += ["-p", f"127.0.0.1:{self.auth.ports[proto]}:{port}"]
        container = subprocess.check_output(command + ["cauce-m8-mail"], text=True).strip()
        self.stack.callback(subprocess.run, ["docker", "stop", "-t", "1", container], stdout=subprocess.DEVNULL)
        for port in self.auth.ports.values():
            def ready():
                with tcp(port) as stream:
                    return bool(stream.recv(256))
            try:
                eventual(ready, seconds=20)
            except AssertionError:
                self.fail(subprocess.check_output(["docker", "logs", container], stderr=subprocess.STDOUT, text=True))
        versions = subprocess.check_output(["docker", "exec", container, "sh", "-c",
                                            "dovecot --version; postconf -h mail_version"], text=True)
        print("backend versions", versions.strip().replace("\n", " / "), flush=True)

    def reference(self):
        version = subprocess.run([NGINX_MAIL, "-V"], capture_output=True, text=True, check=True).stderr
        self.assertIn("nginx/1.30.8\n", version)
        self.assertIn("--with-mail_ssl_module", version)
        ports = {proto: free_port() for proto in MAILD}
        config = self.tmp / "nginx.conf"
        servers = []
        for proto, port in ports.items():
            options = {"smtp": "smtp_auth plain login; proxy_smtp_auth on;",
                       "imap": "imap_auth plain login;", "pop3": "pop3_auth plain;"}[proto]
            servers.append(f"server {{ listen 127.0.0.1:{port}; protocol {proto}; {options} }}")
        config.write_text(f"""
daemon off;
master_process off;
error_log stderr notice;
pid {self.tmp}/nginx.pid;
events {{ worker_connections 128; }}
mail {{
    server_name localhost;
    auth_http http://127.0.0.1:{self.auth.server_port}/auth;
    starttls only;
    ssl_certificate {self.cert};
    ssl_certificate_key {self.key};
    xclient off;
    {" ".join(servers)}
}}
""")
        log = config.with_suffix(".log")
        start_process(self.stack, [NGINX_MAIL, "-p", str(self.tmp), "-c", str(config)], log)
        def ready():
            with tcp(ports["smtp"]) as stream:
                return stream.recv(256).startswith(b"220")
        try:
            eventual(ready)
        except AssertionError:
            self.fail(log.read_text())
        return ports

    def smtp(self, port, implicit=False):
        client = (smtplib.SMTP_SSL("127.0.0.1", port, timeout=4, context=self.tls) if implicit
                  else smtplib.SMTP("127.0.0.1", port, timeout=4))
        self.stack.callback(client.close)
        return client

    def exercise(self, ports, implicit=False):
        with self.smtp(ports["smtp"], implicit) as client:
            self.assertEqual(client.ehlo()[0], 250)
            if not implicit:
                self.assertTrue(client.has_extn("starttls"))
                self.assertFalse(client.has_extn("auth"))
                self.assertEqual(client.starttls(context=self.tls)[0], 220)
                self.assertEqual(client.ehlo()[0], 250)
            self.assertIn("PLAIN", client.esmtp_features["auth"].upper())
            self.assertEqual(client.login(USER, PASSWORD)[0], 235)
            self.assertEqual(client.noop()[0], 250)
        client = (imaplib.IMAP4_SSL("127.0.0.1", ports["imap"], ssl_context=self.tls, timeout=4)
                  if implicit else imaplib.IMAP4("127.0.0.1", ports["imap"], timeout=4))
        with client:
            if not implicit:
                self.assertIn("STARTTLS", client.capabilities)
                self.assertEqual(client.starttls(self.tls)[0], "OK")
            self.assertEqual(client.login(USER, PASSWORD)[0], "OK")
            self.assertEqual(client.select("INBOX"), ("OK", [b"0"]))
        client = (poplib.POP3_SSL("127.0.0.1", ports["pop3"], context=self.tls, timeout=4)
                  if implicit else poplib.POP3("127.0.0.1", ports["pop3"], timeout=4))
        try:
            if not implicit:
                self.assertIn("STLS", client.capa())
                self.assertTrue(client.stls(self.tls).startswith(b"+OK"))
            self.assertTrue(client.user(USER).startswith(b"+OK"))
            self.assertTrue(client.pass_(PASSWORD).startswith(b"+OK"))
            self.assertEqual(client.stat(), (0, 0))
            client.quit()
        finally:
            client.close()

    def test_tls_gate_relay_denial_and_fragmented_transition(self):
        self.start()
        with self.smtp(self.ports["smtp"]) as client:
            self.assertEqual(client.ehlo()[0], 250)
            for command in ("MAIL FROM:<test@fixture.test>", "RCPT TO:<test@fixture.test>", "AUTH PLAIN YQ=="):
                self.assertEqual(client.docmd(command)[0], 530)
            client.starttls(context=self.tls)
            self.assertEqual(client.docmd("AUTH PLAIN YQ==")[0], 503)
            client.ehlo()
            self.assertEqual(client.docmd("DATA")[0], 530)
            self.assertEqual(client.docmd("AUTH PLAIN YQ==")[0], 501)
        self.assertEqual(self.auth.requests, [])
        for proto, request in {"smtp": b"STARTTLS\r\nNOOP\r\n", "imap": b"a STARTTLS\r\nb NOOP\r\n",
                               "pop3": b"STLS\r\nUSER fixture\r\n"}.items():
            with tcp(self.ports[proto]) as stream:
                self.assertTrue(stream.recv(256))
                stream.sendall(request)
                response = stream.recv(256)
                if response:
                    self.assertIn(b"TLS", response)
                    self.assertEqual(stream.recv(256), b"")
        with tcp(self.ports["imap"]) as stream:
            self.assertTrue(stream.recv(256).startswith(b"* OK"))
            for fragment in (b"x STA", b"RTTLS\r", b"\n"):
                stream.sendall(fragment)
            self.assertTrue(stream.recv(256).startswith(b"x OK"))
            with self.tls.wrap_socket(stream, server_hostname="localhost") as encrypted:
                encrypted.sendall(b"A LOGIN fixture {42}\r\n")
                self.assertTrue(encrypted.recv(256).startswith(b"A BAD"))
        self.assertEqual(self.auth.requests, [])

    def test_auth_http_rejection_failure_allowlist_redaction_and_reload(self):
        proc = self.start("implicit")
        for mode, expected in (("reject_bad", 535), ("duplicate", 454), ("redirect", 454),
                               ("outside", 454), ("approve", 454)):
            with self.subTest(mode=mode), self.smtp(self.ports["smtp"], True) as client:
                self.auth.mode = mode
                client.ehlo()
                password = "confidential % fixture" if mode in ("approve", "reject_bad") else PASSWORD
                auth = base64.b64encode(f"\0{USER}\0{password}".encode()).decode()
                self.assertEqual(client.docmd("AUTH PLAIN " + auth)[0], expected)
        self.assertEqual(len(self.auth.requests), 5)
        self.assertEqual(self.auth.requests[0], {"protocol": "smtp", "ssl": "on", "method": "plain",
                                                 "peer": "127.0.0.1", "attempt": 1})
        config = self.configuration("implicit")
        config["proxy"]["mail"][0]["max_connections"] = 42
        self.config.write_text(json.dumps(config))
        proc.send_signal(signal.SIGHUP)
        eventual(lambda: "reload_rejected" in self.log.read_text())
        self.assertNotIn("confidential", self.log.read_text())
        self.assertNotIn(PASSWORD, self.log.read_text())
        with self.smtp(self.ports["smtp"], True) as client:
            self.assertEqual(client.ehlo()[0], 250)

    def test_connection_admission_auth_deadline_and_protocol_bounds(self):
        self.start(max_connections=1, auth_timeout_ms=700)
        with tcp(self.ports["pop3"]) as first:
            self.assertTrue(first.recv(256).startswith(b"+OK"))
            with tcp(self.ports["pop3"]) as second:
                self.assertEqual(second.recv(256), b"")
            self.assertEqual(first.recv(256), b"")
        with tcp(self.ports["imap"]) as stream:
            self.assertTrue(stream.recv(256).startswith(b"* OK"))
            stream.sendall(b"x " + b"A" * 2048 + b"\r\n")
            try:
                self.assertEqual(stream.recv(256), b"")
            except ConnectionResetError:
                pass
        self.assertEqual(self.auth.requests, [])

    def test_backend_tls_precedes_credential_transmission(self):
        class Origin(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"+OK encrypted backend\r\n")
                for expected in (f"USER {USER}", f"PASS {PASSWORD}", "STAT"):
                    actual = self.rfile.readline(2048)
                    self.server.commands.append(actual)
                    if actual != (expected + "\r\n").encode():
                        return
                    self.wfile.write(b"+OK 0 0\r\n")

        origin = start_server(self.stack, ThreadTcp, Origin)
        origin.commands = []
        secure = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        secure.load_cert_chain(self.cert, self.key)
        origin.socket = secure.wrap_socket(origin.socket, server_side=True)
        port = origin.server_address[1]
        self.auth.ports["pop3"] = port
        config = self.configuration("implicit")
        config["proxy"]["mail"][2]["backends"][0]["tls"] = {
            "server_name": "localhost", "ca_file": str(self.cert)}
        dns = start_server(self.stack, socketserver.UDPServer, Dns)
        dns.family = socket.AF_INET
        config["proxy"]["dns"] = {"servers": [f"127.0.0.1:{dns.server_address[1]}"]}
        config["proxy"]["mail"][2]["backends"][0]["dns_address"] = f"origin.test:{port}"
        # Validate and launch exactly the edited configuration.
        self.start(configuration=config)
        client = poplib.POP3_SSL("127.0.0.1", self.ports["pop3"], context=self.tls, timeout=4)
        self.stack.callback(client.close)
        client.user(USER)
        client.pass_(PASSWORD)
        self.assertEqual(client.stat(), (0, 0))
        self.assertEqual(len(origin.commands), 3)

        bad_config = self.configuration("implicit")
        bad_config["proxy"]["admin"] = f"127.0.0.1:{free_port()}"
        bad_listeners = self.ports.copy()
        for settings in bad_config["proxy"]["mail"]:
            bad_listeners[settings["protocol"]] = free_port()
            settings["listen"] = f"127.0.0.1:{bad_listeners[settings['protocol']]}"
        bad_config["proxy"]["mail"][2]["backends"][0]["tls"] = {
            "server_name": "unexpected.test", "ca_file": str(self.cert)}
        self.start(configuration=bad_config, config_file="bad-backend-name")
        client = poplib.POP3_SSL("127.0.0.1", bad_listeners["pop3"], context=self.tls, timeout=4)
        self.stack.callback(client.close)
        client.user(USER)
        with self.assertRaises(poplib.error_proto) as result:
            client.pass_(PASSWORD)
        self.assertIn(b"SYS/TEMP", result.exception.args[0])
        self.assertEqual(len(origin.commands), 3)

    def test_dns_rotation_preserves_authenticated_pop3_session(self):
        class Origin(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"+OK fixture\r\n")
                for credential in (f"USER {USER}", f"PASS {PASSWORD}"):
                    if self.rfile.readline(2048) != (credential + "\r\n").encode():
                        return
                    self.wfile.write(b"+OK accepted\r\n")
                while command := self.rfile.readline(2048):
                    if command == b"STAT\r\n":
                        self.wfile.write(f"+OK {self.server.marker} 0\r\n".encode())
                    else:
                        return

        class ThreadTcp6(ThreadTcp):
            address_family = socket.AF_INET6

            def server_bind(self):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                super().server_bind()

        ipv4 = start_server(self.stack, ThreadTcp, Origin)
        backend_port = ipv4.server_address[1]
        ipv6 = start_server(self.stack, ThreadTcp6, Origin, host="::1", port=backend_port)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(self.cert, self.key)
        for origin, marker in [(ipv4, 4), (ipv6, 6)]:
            origin.marker = marker
            origin.socket = tls.wrap_socket(origin.socket, server_side=True)
        self.auth.ports["pop3"] = backend_port
        dns = start_server(self.stack, socketserver.UDPServer, Dns)
        dns.family = socket.AF_INET
        config = self.configuration("implicit")
        config["proxy"]["dns"] = {"servers": [f"127.0.0.1:{dns.server_address[1]}"]}
        backend = config["proxy"]["mail"][2]["backends"][0]
        backend["dns_address"] = f"origin.test:{backend_port}"
        backend["tls"] = {"server_name": "localhost", "ca_file": str(self.cert)}
        self.start(configuration=config)

        def authenticate():
            client = poplib.POP3_SSL("127.0.0.1", self.ports["pop3"], context=self.tls, timeout=4)
            self.stack.callback(client.close)
            client.user(USER)
            client.pass_(PASSWORD)
            return client

        existing = authenticate()
        self.assertEqual(existing.stat(), (4, 0))
        dns.family = socket.AF_INET6
        time.sleep(1.15)
        self.assertEqual(authenticate().stat(), (6, 0))
        self.assertEqual(existing.stat(), (4, 0))

    def test_reload_adds_and_removes_listeners_with_authenticated_pop3(self):
        class Origin(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"+OK greeting\r\n")
                for expected in (b"USER", b"PASS", b"STAT", b"QUIT"):
                    command = self.rfile.readline(2048)
                    if not command.startswith(expected):
                        return
                    self.wfile.write(b"+OK 0 0\r\n")

        origin = start_server(self.stack, ThreadTcp, Origin)
        self.auth.ports["pop3"] = origin.server_address[1]
        config = self.configuration("implicit")
        original = config["proxy"]["mail"]
        added_port = free_port()
        extra = dict(original[0], listen=f"127.0.0.1:{added_port}")
        proc = self.start(configuration=config)
        client = poplib.POP3_SSL("127.0.0.1", self.ports["pop3"], context=self.tls, timeout=4)
        self.stack.callback(client.close)
        client.user(USER)
        client.pass_(PASSWORD)

        config["proxy"]["mail"] = [item for item in original if item["protocol"] != "pop3"] + [extra]
        replacement = self.config.with_suffix(".tmp")
        replacement.write_text(json.dumps(config))
        replacement.replace(self.config)
        proc.send_signal(signal.SIGHUP)
        eventual(lambda: '"config_version":2' in self.log.read_text())
        with self.smtp(added_port, True) as added:
            self.assertEqual(added.ehlo()[0], 250)
        def retired():
            try:
                with tcp(self.ports["pop3"]):
                    return False
            except ConnectionError:
                return True
        eventual(retired)
        self.assertEqual(client.stat(), (0, 0))
        self.assertTrue(client.quit().startswith(b"+OK"))

    def test_sigterm_drains_authenticated_sessions(self):
        class Origin(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"+OK greeting\r\n")
                for expected in (b"USER", b"PASS", b"STAT", b"QUIT"):
                    command = self.rfile.readline(2048)
                    if not command.startswith(expected):
                        return
                    self.wfile.write(b"+OK 0 0\r\n")

        origin = start_server(self.stack, ThreadTcp, Origin)
        self.auth.ports["pop3"] = origin.server_address[1]
        proc = self.start()
        client = poplib.POP3("127.0.0.1", self.ports["pop3"], timeout=4)
        self.stack.callback(client.close)
        client.stls(self.tls)
        client.user(USER)
        client.pass_(PASSWORD)
        proc.send_signal(signal.SIGTERM)

        def listener_closed():
            try:
                with tcp(self.ports["pop3"]):
                    return False
            except (ConnectionRefusedError, ConnectionResetError):
                return True

        eventual(listener_closed)
        self.assertEqual(client.stat(), (0, 0))
        self.assertTrue(client.quit().startswith(b"+OK"))
        self.assertEqual(proc.wait(5), 0)

    @unittest.skipUnless(os.environ.get("CAUCE_MAIL_FIXTURE"), "build tests/mail/Dockerfile and set CAUCE_MAIL_FIXTURE=1")
    def test_postfix_dovecot_and_optional_nginx(self):
        self.origin()
        self.start()
        self.exercise(self.ports)
        if NGINX_MAIL:
            self.exercise(self.reference())
        print(f"SMTP, IMAP and POP3 STARTTLS with Postfix/Dovecot, NGINX differential {bool(NGINX_MAIL)}", flush=True)
        self.assertEqual(len(self.auth.requests), 6 if NGINX_MAIL else 3)

    @unittest.skipUnless(os.environ.get("CAUCE_MAIL_FIXTURE"), "build tests/mail/Dockerfile and set CAUCE_MAIL_FIXTURE=1")
    def test_implicit_tls_to_real_origins(self):
        self.origin()
        self.start("implicit")
        self.exercise(self.ports, implicit=True)
        with self.smtp(self.ports["smtp"], True) as client:
            client.ehlo()
            client.user, client.password = USER, PASSWORD
            self.assertEqual(client.auth("LOGIN", client.auth_login)[0], 235)
        client = imaplib.IMAP4_SSL("127.0.0.1", self.ports["imap"], ssl_context=self.tls, timeout=4)
        with client:
            self.assertEqual(client.authenticate("PLAIN", lambda _: f"\0{USER}\0{PASSWORD}".encode())[0], "OK")
        with self.smtp(self.ports["smtp"], True) as client:
            client.ehlo()
            self.auth.mode = "approve"
            client.user, client.password = USER, "wrong-by-design"
            with self.assertRaises(smtplib.SMTPAuthenticationError) as result:
                client.auth("PLAIN", client.auth_plain)
            self.assertEqual(result.exception.smtp_code, 454)


if __name__ == "__main__":
    unittest.main(verbosity=2)
