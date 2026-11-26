#!/usr/bin/env python3
"""Stage and publish Stream sockets while preserving admitted sessions."""
import json
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest

from content_handlers import settings, wait
from integration import BIN, port


class TcpEcho(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            while data := self.request.recv(256):
                self.request.sendall(data)
        except ConnectionError:
            pass


class UdpEcho(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(data, self.client_address)


class TcpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True


class Reload(unittest.TestCase):
    def start_fixture(self, server):
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_address[1]

    def test_atomic_addition_conflict_removal_and_draining_udp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        config, log = directory / "gateway.yaml", directory / "stderr"
        tcp_origin = self.start_fixture(TcpServer(("127.0.0.1", 0), TcpEcho))
        udp_origin = self.start_fixture(socketserver.UDPServer(("127.0.0.1", 0), UdpEcho))
        old_tcp, spare = port(), port()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            old_udp = sock.getsockname()[1]

        data = settings()
        data.update(listen=f"127.0.0.1:{port()}", admin=f"127.0.0.1:{port()}", routes=[], upstreams=[])
        data["routes"] = [{"id":"health", "host":"localhost", "path_prefix":"/",
                           "content":{"type":"direct", "status":200, "body":"ready"}}]
        groups = [dict(id=group, endpoints=[dict(address=f"127.0.0.1:{backend}")])
                  for group, backend in [("tcp", tcp_origin), ("udp", udp_origin)]]
        def tcp_binding(tcp_port):
            return dict(listen=f"127.0.0.1:{tcp_port}", upstream="tcp")
        udp_binding = dict(listen=f"127.0.0.1:{old_udp}", upstream="udp",
                           options=dict(idle_timeout="500ms", session_timeout="3s"))
        def write(tcp, udp):
            data["stream"] = dict(upstreams=groups, tcp=tcp, udp=udp)
            temporary = config.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version":1, "proxy":data}))
            temporary.replace(config)

        def tcp_dial(tcp_port):
            return socket.create_connection(("127.0.0.1", tcp_port), timeout=0.4)

        def udp_dial():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.25)
            sock.connect(("127.0.0.1", old_udp))
            return sock

        def echo(sock, data):
            sock.sendall(data)
            self.assertEqual(sock.recv(256), data)

        write([tcp_binding(old_tcp)], [udp_binding])
        with log.open("w") as stderr:
            proxy = subprocess.Popen([BIN, config], stderr=stderr, cwd=directory)
        def terminate():
            if proxy.poll() is None:
                proxy.terminate()
                proxy.wait(timeout=8)
        self.addCleanup(terminate)
        wait(log, '"started"', proxy)

        with tcp_dial(old_tcp) as retained, udp_dial() as associated:
            echo(retained, b"initial")
            echo(associated, b"initial")
            with TcpServer(("127.0.0.1", 0), TcpEcho) as conflict:
                busy = conflict.server_address[1]
                write([tcp_binding(old_tcp), tcp_binding(spare), tcp_binding(busy)], [udp_binding])
                proxy.send_signal(signal.SIGHUP)
                wait(log, '"reload_rejected"', proxy)
                with self.assertRaises(OSError):
                    tcp_dial(spare).close()
                echo(retained, b"rollback")
            proxy.send_signal(signal.SIGHUP)
            wait(log, '"config_version":2', proxy)
            with tcp_dial(spare) as added, tcp_dial(busy) as recovered:
                echo(added, b"added")
                echo(recovered, b"recovered")
            echo(associated, b"reuse")
            write([tcp_binding(spare), tcp_binding(busy)], [])
            proxy.send_signal(signal.SIGHUP)
            wait(log, '"config_version":3', proxy)
            deadline = time.monotonic() + 1
            while True:
                try:
                    tcp_dial(old_tcp).close()
                except OSError:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            echo(retained, b"draining")
            echo(associated, b"association preserved")
            with udp_dial() as new_peer:
                with self.assertRaises(OSError):
                    echo(new_peer, b"new peer")
        time.sleep(0.65)
        write([tcp_binding(old_tcp), tcp_binding(spare), tcp_binding(busy)], [udp_binding])
        proxy.send_signal(signal.SIGHUP)
        wait(log, '"config_version":4', proxy)
        with tcp_dial(old_tcp) as rebound, udp_dial() as fresh:
            echo(rebound, b"rebound")
            echo(fresh, b"fresh UDP")


if __name__ == "__main__":
    unittest.main(verbosity=2)
