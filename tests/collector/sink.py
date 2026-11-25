#!/usr/bin/env python3
"""Local proposal-1 fixture. This is NOT a Connectivity service or auth integration."""
import argparse
import json
from pathlib import Path
import socket
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dir', required=True)
    p.add_argument('--port', type=int, default=9443)
    a = p.parse_args()
    root = Path(a.dir)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4 * 1024 * 1024:
                self.send_error(413)
                return
            payload = self.rfile.read(length)
            try:
                batch = json.loads(payload)
                assert batch['contract_version'] == 'proposal-1'
                assert isinstance(batch['events'], list) and batch['events']
            except (ValueError, KeyError, AssertionError):
                self.send_error(400)
                return
            control = json.loads((root / 'control.json').read_text())
            status = control.get('status', 200)
            # Synthetic credentials only. Authorization outcome is recorded, never token bytes.
            authorized = self.headers.get('Authorization') == 'Bearer ' + control.get('token', 'fixture-token-v1')
            if not authorized:
                status = 401
            with lock:
                with (root / 'received.jsonl').open('a') as f:
                    f.write(json.dumps({'batch': batch, 'status': status, 'authorized': authorized,
                                        'time': time.time(), 'path': self.path}) + '\n')
                    f.flush()
                if status == 200:
                    # Dedup demonstration belongs to this fixture only.
                    ids_path = root / 'dedup.json'
                    ids = set(json.loads(ids_path.read_text())) if ids_path.exists() else set()
                    ids.update(e['event_id'] for e in batch['events'])
                    ids_path.write_text(json.dumps(sorted(ids)))
            if control.get('drop_ack'):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if control.get('delay_ms'):
                time.sleep(control['delay_ms'] / 1000)
            ack = {'batch_id': batch['batch_id'], 'accepted_count': len(batch['events'])}
            if control.get('wrong_id'):
                ack['batch_id'] = 'wrong-batch'
            if control.get('wrong_count'):
                ack['accepted_count'] += 1
            if control.get('extra_ack'):
                ack[control['extra_ack']] = control.get('extra_ack_value', 'fixture-value')
            response = json.dumps(ack).encode()
            if control.get('large_ack'):
                response = b' ' * 100000 + response
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            if status in (301, 302, 307, 308):
                self.send_header('Location', 'https://sink.test:9443/redirect-target')
            self.end_headers()
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('0.0.0.0', a.port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / 'server.crt', root / 'server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
