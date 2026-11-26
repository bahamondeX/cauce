#!/usr/bin/env python3
"""Compare Stream TLS termination and verified re-encryption against local nginx."""
import contextlib
import json
import os
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from content_handlers import settings, wait
from integration import BIN, port
from tls_gateway import issue, openssl


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(3)
            with self.server.tls.wrap_socket(self.request, server_side=True) as connection:
                name = dict(item for group in connection.getpeercert()["subject"] for item in group)["commonName"]
                data = connection.recv(20)
                if data:
                    connection.sendall(name.encode() + b":" + data)
                    connection.unwrap().close()
        except (ssl.SSLError, OSError):
            pass


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


def stop(process):
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=8)


class StreamTls(unittest.TestCase):
    def test_stream_tls(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            directory = Path(temp)
            for ca in ("trusted", "other"):
                openssl(directory, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                        "-subj", f"/CN={ca}", "-addext", "basicConstraints=critical,CA:TRUE",
                        "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
                        "-keyout", f"{ca}-key.pem", "-out", f"{ca}.pem")
            issue(directory, 1, "default", "trusted", "serverAuth", "default.test")
            issue(directory, 2, "alternate", "trusted", "serverAuth", "named.test")
            issue(directory, 3, "backend", "trusted", "serverAuth", "backend.test")
            issue(directory, 4, "client", "trusted", "clientAuth")
            issue(directory, 5, "upstream-client", "trusted", "clientAuth")
            issue(directory, 6, "untrusted", "other", "clientAuth")
            origin = stack.enter_context(Server(("127.0.0.1", 0), Echo))
            origin.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            origin.tls.load_cert_chain(directory / "backend.pem", directory / "backend-key.pem")
            origin.tls.load_verify_locations(directory / "trusted.pem")
            origin.tls.verify_mode = ssl.CERT_REQUIRED
            threading.Thread(target=origin.serve_forever, daemon=True).start()
            stack.callback(origin.shutdown)
            backend_port = origin.server_address[1]
            good, bad = self.start_cauce(directory, stack, backend_port)
            self.exercise(directory, good, bad)
            with socket.create_connection(("127.0.0.1", good), timeout=2) as stalled:
                self.assertEqual(stalled.recv(1), b"")
            nginx = os.environ.get("NGINX")
            if nginx:
                version = subprocess.run([nginx, "-v"], check=True, capture_output=True).stderr
                self.assertEqual(version.strip(), b"nginx version: nginx/1.30.8")
                reference_good, reference_bad = self.start_nginx(nginx, directory, stack, backend_port)
                self.exercise(directory, reference_good, reference_bad)
                print("NGINX 1.30.8 and Cauce matched SNI, mTLS, and upstream hostname verification")

    def exchange(self, directory, listen, name, credentials):
        context = ssl.create_default_context(cafile=directory / "trusted.pem")
        if credentials:
            context.load_cert_chain(directory / f"{credentials}.pem", directory / f"{credentials}-key.pem")
        with socket.create_connection(("127.0.0.1", listen), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname=name) as connection:
                connection.sendall(b"payload")
                return connection.recv(128)

    def exercise(self, directory, good, bad):
        self.assertEqual(self.exchange(directory, good, "default.test", "client"), b"upstream-client:payload")
        self.assertEqual(self.exchange(directory, good, "named.test", "client"), b"upstream-client:payload")
        for credentials in (None, "untrusted"):
            try:
                self.assertEqual(self.exchange(directory, good, "default.test", credentials), b"")
            except (ConnectionError, ssl.SSLError):
                pass
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.exchange(directory, good, "uncovered.test", "client")
        try:
            self.assertEqual(self.exchange(directory, bad, "default.test", "client"), b"")
        except (ConnectionError, ssl.SSLError):
            pass

    def start_cauce(self, directory, stack, backend):
        good, bad = port(), port()
        config = settings()
        config.pop("listen")
        config.update(admin=f"127.0.0.1:{port()}", upstreams=[], routes=[])
        def upstream(id, hostname):
            return dict(id=id, endpoints=[dict(address=f"127.0.0.1:{backend}")],
                        tls=dict(server_name=hostname, ca_file="trusted.pem",
                                 client_certificate=dict(cert_file="upstream-client.pem", key_file="upstream-client-key.pem")))
        tls = dict(cert_file="default.pem", key_file="default-key.pem", client_ca_file="trusted.pem",
                   sni={"named.test": dict(cert_file="alternate.pem", key_file="alternate-key.pem")},
                   session_resumption=False)
        config["stream"] = {
            "upstreams": [upstream("echo", "backend.test"), upstream("bad", "wrong.test")],
            "tcp": [
                dict(listen=f"127.0.0.1:{good}", upstream="echo", tls=tls, tls_handshake_timeout="300ms"),
                dict(listen=f"127.0.0.1:{bad}", upstream="bad", tls=tls, tls_handshake_timeout="300ms"),
            ],
        }
        path = directory / "cauce.yaml"
        path.write_text(json.dumps({"version": 1, "proxy": config}))
        subprocess.run([BIN, "check", path], check=True, cwd=directory)
        log = directory / "cauce.log"
        with log.open("w") as output:
            process = subprocess.Popen([BIN, path], cwd=directory, stderr=output)
        stack.callback(stop, process)
        wait(log, '"started"', process)
        return good, bad

    def start_nginx(self, nginx, directory, stack, backend):
        good, bad = port(), port()
        path = directory / "nginx.conf"
        def server(listen, cert, name, upstream_name, default=""):
            return f"""
                server {{
                    listen 127.0.0.1:{listen} ssl {default};
                    server_name {name};
                    ssl_certificate {cert}.pem;
                    ssl_certificate_key {cert}-key.pem;
                    ssl_verify_client on;
                    ssl_client_certificate trusted.pem;
                    proxy_pass 127.0.0.1:{backend};
                    proxy_ssl on;
                    proxy_ssl_verify on;
                    proxy_ssl_server_name on;
                    proxy_ssl_name {upstream_name};
                    proxy_ssl_trusted_certificate trusted.pem;
                    proxy_ssl_certificate upstream-client.pem;
                    proxy_ssl_certificate_key upstream-client-key.pem;
                }}
            """
        path.write_text("daemon off; master_process off; error_log stderr notice; pid reference.pid; "
                        "events {} stream {" + server(good, "default", "default.test", "backend.test", "default_server")
                        + server(good, "alternate", "named.test", "backend.test")
                        + server(bad, "default", "default.test", "wrong.test") + "}")
        log = directory / "reference.log"
        with log.open("w") as output:
            process = subprocess.Popen([nginx, "-p", str(directory), "-c", str(path)], cwd=directory, stderr=output,
                                       env={key: value for key, value in os.environ.items() if key != "NGINX"})
        stack.callback(stop, process)
        wait(log, "event method", process)
        return good, bad


if __name__ == "__main__":
    unittest.main(verbosity=2)
