#!/usr/bin/env python3
"""Black-box HTTP tests, real sockets and subprocesses; no mock proxy transport."""
import concurrent.futures
import errno
import http.client
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get('CAUCE_BIN', ROOT / 'target/debug/cauce'))


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Backend(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1
            self.connection_id = self.server.connections

    def log_message(self, *args):
        pass

    def do_POST(self):
        with self.server.lock:
            self.server.posts += 1
            self.server.last_framing = (self.headers.get("Content-Length"), self.headers.get("Transfer-Encoding"))
        if self.path == '/early':
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.headers.get('Transfer-Encoding') == 'chunked': 
            parts = []
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if not size:
                    self.rfile.readline()
                    break
                parts.append(self.rfile.read(size))
                self.server.upload_progress.set()
                self.rfile.read(2)
            data = b''.join(parts)
        else:
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with self.server.lock:
            self.server.hits += 1
        try:
            if self.path.startswith('/sse'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                for i in range(3):
                    b = f'data: {i}\n\n'.encode()
                    self.wfile.write(f'{len(b):x}\r\n'.encode() + b + b'\r\n')
                    self.wfile.flush()
                    time.sleep(.08)
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
                return
            if self.path.startswith('/held'):
                self.server.held.set()
                if not self.server.release.wait(timeout=3):
                    self.close_connection = True
                    return
            if self.path.startswith('/close'):
                self.close_connection = True
                return
            if self.path.startswith('/slow'):
                time.sleep(.7)
            if self.path.startswith('/stall'):
                self.send_response(200)
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.write(b'1\r\na\r\n')
                self.wfile.flush()
                time.sleep(1)
                self.wfile.write(b'0\r\n\r\n')
                return
            if self.path.startswith('/cancel'):
                self.send_response(200)
                self.send_header('Content-Length', '1000000')
                self.end_headers()
                for _ in range(1000):
                    self.wfile.write(b'x' * 1000)
                    self.wfile.flush()
                    time.sleep(.005)
                return
            data = json.dumps({'connection': self.connection_id, 'backend': self.server.label,
                               'forwarded': self.headers.get('X-Forwarded-For'),
                               'secret': self.headers.get('X-Secret'), 'traceparent': self.headers.get('traceparent'),
                               'drop_me': self.headers.get('X-Drop-Me')}).encode()
            self.send_response(200)
            if self.path == '/policy':
                self.send_header('X-Origin-Debug', 'private')
                self.send_header('X-Policy', 'from-origin')
            if self.path.startswith('/connection-close'):
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, label):
        super().__init__(('127.0.0.1', 0), Backend)
        self.label = label
        self.connections = self.hits = self.posts = 0
        self.last_framing = None
        self.upload_progress = threading.Event()
        self.held = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()
    def handle_error(self, *args):
        pass


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.backends = [Server('a'), Server('b')]
        self.listen, self.admin = port(), port()
        config = (ROOT / 'examples/cauce.toml').read_text()
        config = config.replace('127.0.0.1:8080', f'127.0.0.1:{self.listen}').replace('localhost:8080', f'localhost:{self.listen}')
        config = config.replace('127.0.0.1:9090', f'127.0.0.1:{self.admin}')
        for old, backend in zip([8000, 8001], self.backends):
            config = config.replace(f'127.0.0.1:{old}', f'127.0.0.1:{backend.server_port}')
        config = config.replace('requests = 256', 'requests = 2').replace('stream_idle_ms = 15000', 'stream_idle_ms = 300')
        config = config.replace('client_headers_ms = 5000', 'client_headers_ms = 200').replace('shutdown_ms = 5000', 'shutdown_ms = 400')
        config = config.replace('upstreams = ["python", "node"]', 'upstreams = ["python"]')
        if self._testMethodName == 'test_bad_upload_does_not_withdraw_healthy_backend':
            config = config.replace('max_body_bytes = 16777216', 'max_body_bytes = 8')
        if self._testMethodName == 'test_pool_capacity_wait_is_bounded':
            config = config.replace('connections = 32', 'connections = 1').replace('idle_connections = 16', 'idle_connections = 1')
        if self._testMethodName == 'test_idle_and_max_age_retirement':
            config = config.replace('idle_connection_ms = 10000', 'idle_connection_ms = 100').replace('max_connection_age_ms = 60000', 'max_connection_age_ms = 250')
        if self._testMethodName == 'test_log_rotation_and_retention':
            config = config.replace('rotate_bytes = 16777216', 'rotate_bytes = 4096').replace('retained_files = 3', 'retained_files = 2')
        if self._testMethodName == 'test_disabled_access_log_preserves_metrics_and_request_identity':
            config = config.replace('enabled = true', 'enabled = false')
        if self._testMethodName == 'test_burst_reuse_and_eviction_during_reload':
            config = config.replace('requests = 2\n', 'requests = 16\n')
            config = config.replace('connections = 64', 'connections = 4').replace('idle_connections = 32', 'idle_connections = 4')
            config = config.replace('connections = 32', 'connections = 4').replace('idle_connections = 16', 'idle_connections = 4')
            config = config.replace('acquire_ms = 250', 'acquire_ms = 1000')
            config = config.replace('client_headers_ms = 200', 'client_headers_ms = 2000')
        self.path = self.base / 'config.toml'
        if self._testMethodName in {'test_yaml_reload_routes_and_rollback', 'test_header_policies_are_route_scoped'}:
            self.path = self.base / 'config.yaml'
            config = (ROOT / 'examples/cauce.yaml').read_text()
            config = config.replace('127.0.0.1:8080', f'127.0.0.1:{self.listen}')
            config = config.replace('localhost:8080', f'localhost:{self.listen}')
            config = config.replace('127.0.0.1:9090', f'127.0.0.1:{self.admin}')
            for old, backend in zip([8000, 8001], self.backends):
                config = config.replace(f'127.0.0.1:{old}', f'127.0.0.1:{backend.server_port}')
            config = config.replace('upstreams: [python, node]', 'upstreams: [python]')
        self.path.write_text(config)
        self.stderr = open(self.base / 'stderr.jsonl', 'w')
        self.proc = subprocess.Popen([str(BIN), str(self.path)], cwd=self.base, env={**os.environ, "CAUCE_ADMIN_TOKEN": "test-only-admin-token-at-least-32-bytes"}, stderr=self.stderr, stdout=subprocess.DEVNULL)
        for _ in range(100):
            try:
                if self.request('/ready', admin=True)[0] == 200:
                    break
            except OSError:
                pass
            if self.proc.poll() is not None:
                self.fail((self.base / 'stderr.jsonl').read_text())
            time.sleep(.02)
        else:
            self.fail('readiness timeout')

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        self.stderr.close()
        if os.environ.get('CAUCE_EVIDENCE_DIR'):
            dest = Path(os.environ['CAUCE_EVIDENCE_DIR']) / self._testMethodName
            dest.mkdir(parents=True, exist_ok=True)
            for path in self.base.rglob('*.jsonl'):
                shutil.copyfile(path, dest / path.name)
        for b in self.backends:
            b.shutdown()
            b.server_close()
        self.temp.cleanup()

    def request(self, path='/', *, method='GET', body=None, headers=None, admin=False):
        c = http.client.HTTPConnection('127.0.0.1', self.admin if admin else self.listen, timeout=3)
        c.request(method, path, body=body, headers={'Host': f'localhost:{self.listen}', **({'Authorization': 'Bearer test-only-admin-token-at-least-32-bytes'} if admin else {}), **(headers or {})})
        r = c.getresponse()
        result = (r.status, r.read(), dict(r.getheaders()))
        c.close()
        return result

    def raw(self, data, wait=0):
        with socket.create_connection(('127.0.0.1', self.listen), timeout=2) as s:
            s.sendall(data)
            if wait:
                time.sleep(wait)
            try:
                return s.recv(8192)
            except ConnectionResetError:
                return b''

    def logs(self):
        time.sleep(.12)
        path = self.base / 'logs/access.jsonl'
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def test_pool_reuse_across_new_client_connections_and_logs(self):
        one = json.loads(self.request('/?token=do-not-log', headers={'Cookie': 'secret-cookie'})[1])
        two = json.loads(self.request('/')[1])
        self.assertEqual(one['connection'], two['connection'])
        self.assertEqual(self.backends[0].connections, 1)
        logs = self.logs()
        self.assertFalse(logs[0]['attempts'][0]['reused'])
        self.assertTrue(logs[1]['attempts'][0]['reused'])
        self.assertIsNone(logs[1]['attempts'][0]['tcp_ms'])
        self.assertIsNone(logs[1]['attempts'][0]['dns_ms'])
        self.assertNotIn('do-not-log', json.dumps(logs))
        self.assertNotIn('secret-cookie', json.dumps(logs))
        self.assertEqual(logs[0]['outcome'], 'complete')
        metrics = self.request('/metrics', admin=True)[1].decode()
        self.assertIn('cauce_upstream_reused_total 1', metrics)

    def test_disabled_access_log_preserves_metrics_and_request_identity(self):
        import uuid

        trace = '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01'
        status, body, headers = self.request('/', headers={'traceparent': trace})
        self.assertEqual(status, 200)
        self.assertEqual(uuid.UUID(headers['x-request-id']).version, 4)
        self.assertEqual(json.loads(body)['traceparent'], trace)
        second = self.request('/')
        self.assertEqual(second[0], 200)
        self.assertNotEqual(headers['x-request-id'], second[2]['x-request-id'])
        self.assertEqual(self.request('/', headers={'Host': 'missing.test'})[0], 404)
        metrics = self.request('/metrics', admin=True)[1].decode().splitlines()
        for name, value in {
            'requests': 3, 'responses': 2, 'rejected': 1, 'upstream_reused': 1,
            'response_bytes': len(body) + len(second[1]), 'log_dropped': 0, 'log_io_errors': 0,
        }.items():
            self.assertIn(f'cauce_{name}_total {value}', metrics)
        self.assertFalse((self.base / 'logs/access.jsonl').exists())

    def test_routing_framing_hops_and_forwarded(self):
        self.assertEqual(self.request('/', headers={'Host': 'attacker.test'})[0], 404)
        self.assertEqual(self.request('/a/%2fsecret')[0], 400)
        self.assertEqual(self.request('http://attacker.test/')[0], 400)
        r = json.loads(self.request('/', headers={'Connection': 'x-secret', 'X-Secret': 'hide', 'X-Forwarded-For': 'evil', 'traceparent': 'invalid'})[1])
        self.assertEqual(r['forwarded'], '127.0.0.1')
        self.assertIsNone(r['secret'])
        self.assertIsNone(r['traceparent'])
        self.assertEqual(self.request('/', headers={'Upgrade': 'websocket'})[0], 400)
        self.assertEqual(self.request('/', headers={'Upgrade': 'unknown'})[0], 501)

    def test_header_policies_are_route_scoped(self):
        original = self.path.read_text()
        candidate = original + f'''
    - id: policy
      host: "localhost:{self.listen}"
      path_match: exact
      path_prefix: /policy
      balance: round_robin
      upstreams: [python]
      headers:
        request:
          set: {{x-secret: from-config}}
          remove: [X-Drop-Me]
        response:
          set: {{x-policy: from-cauce}}
          remove: [x-origin-debug]
'''
        def reload(text, event):
            self.path.write_text(text)
            self.proc.send_signal(signal.SIGHUP)
            for _ in range(100):
                if event in (self.base / 'stderr.jsonl').read_text():
                    return
                time.sleep(.02)
            self.fail(f'Expected {event}')

        reload(candidate, '"reloaded"')
        incoming = {'X-Secret': 'from-client', 'X-Drop-Me': 'drop', 'X-Forwarded-For': '192.0.2.1'}
        status, payload, headers = self.request('/policy', headers=incoming)
        result = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual((result['secret'], result['drop_me'], result['forwarded']),
                         ('from-config', None, '127.0.0.1'))
        lower = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lower['x-policy'], 'from-cauce')
        self.assertNotIn('x-origin-debug', lower)
        self.assertIn('x-request-id', lower)
        self.assertEqual(json.loads(self.request('/', headers=incoming)[1])['secret'], 'from-client')
        reload(candidate.replace('x-secret: from-config', 'content-length: "500"'), '"reload_rejected"')
        self.assertEqual(json.loads(self.request('/policy', headers=incoming)[1])['secret'], 'from-config')

    def test_request_smuggling_rejected_or_canonicalized(self):
        host = f'Host: localhost:{self.listen}\r\n'.encode()
        cases = [b'Content-Length: 4\r\nTransfer-Encoding: chunked\r\n',
                 b'Content-Length: 0\r\nContent-Length: 4\r\n',
                 b'Transfer-Encoding: gzip, chunked\r\n', b'Connection: content-length\r\nContent-Length: 0\r\n',
                 b'Transfer-Encoding : chunked\r\n']
        for i, h in enumerate(cases):
            result = self.raw(b'POST / HTTP/1.1\r\n' + host + h + b'Connection: close\r\n\r\n0\r\n\r\n')
            if i == 0:
                # Hyper 1.12 removes CL when TE is present. The proxy re-frames the body.
                self.assertIn(b'200', result.split(b'\r\n')[0])
                cl, te = self.backends[0].last_framing
                self.assertFalse(cl is not None and te is not None)
            else:
                self.assertTrue(not result or b'400' in result.split(b'\r\n')[0], (h, result[:200]))
        self.assertEqual(self.backends[0].hits, 0)
        self.assertEqual(self.backends[0].posts, 1)

    def test_slowloris_and_header_limits(self):
        r = self.raw(b'GET / HTTP/1.1\r\nHost: ', wait=.4)
        self.assertTrue(not r or b'408' in r.split(b'\r\n')[0], r)
        r = self.raw(f'GET / HTTP/1.1\r\nHost: localhost:{self.listen}\r\nX: '.encode() + b'x'*40000 + b'\r\n\r\n')
        self.assertTrue(not r or b'431' in r.split(b'\r\n')[0], r[:100])

    def test_upload_and_sse_without_event_buffering(self):
        self.assertEqual(self.request('/', method='POST', body=b'hello')[1], b'hello')
        c = http.client.HTTPConnection('127.0.0.1', self.listen, timeout=2)
        c.request('GET', '/sse', headers={'Host': f'localhost:{self.listen}'})
        start = time.monotonic()
        r = c.getresponse()
        self.assertEqual(r.readline(), b'data: 0\n')
        self.assertLess(time.monotonic() - start, .18)
        self.assertIn(b'data: 2', r.read())
        c.close()
        # Chunked upload is streamed and re-framed by Hyper.
        host = f'Host: localhost:{self.listen}\r\n'.encode()
        result = self.raw(b'POST / HTTP/1.1\r\n' + host + b'Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n')
        self.assertIn(b'200', result.split(b'\r\n')[0])

    def test_cancel_and_stream_timeout_discard_upstream(self):
        c = http.client.HTTPConnection('127.0.0.1', self.listen, timeout=2)
        c.request('GET', '/cancel', headers={'Host': f'localhost:{self.listen}'})
        r = c.getresponse()
        self.assertEqual(len(r.read(1000)), 1000)
        c.close()
        time.sleep(.1)
        new = json.loads(self.request('/')[1])
        self.assertGreater(new['connection'], 1)
        c = http.client.HTTPConnection('127.0.0.1', self.listen, timeout=2)
        c.request('GET', '/stall', headers={'Host': f'localhost:{self.listen}'})
        r = c.getresponse()
        with self.assertRaises((http.client.IncompleteRead, ConnectionResetError)):
            r.read()
        c.close()
        self.assertTrue(any(e['outcome'] == 'stream_timeout' for e in self.logs()))

    def test_overload_and_shutdown_deadline(self):
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            pending = [executor.submit(self.request, '/slow') for _ in range(2)]
            time.sleep(.1)
            self.assertEqual(self.request('/')[0], 503)
            for p in pending:
                self.assertEqual(p.result()[0], 200)
        c = http.client.HTTPConnection('127.0.0.1', self.listen, timeout=2)
        c.request('GET', '/cancel', headers={'Host': f'localhost:{self.listen}'})
        c.getresponse()
        start = time.monotonic()
        self.proc.terminate()
        self.proc.wait(timeout=2)
        self.assertLess(time.monotonic() - start, 1.5)
        c.close()

    def test_atomic_reload_and_invalid_reload_preserves_generation(self):
        old = json.loads(self.request('/')[1])
        self.path.write_text(self.path.read_text().replace('upstreams = ["python"]', 'upstreams = ["node"]'))
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(.1)
        new = json.loads(self.request('/')[1])
        self.assertNotEqual(old['backend'], new['backend'])
        self.assertEqual(self.logs()[-1]['config_version'], 2)
        self.path.write_text('invalid configuration')
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(.1)
        self.assertEqual(json.loads(self.request('/')[1])['backend'], 'b')
        self.assertEqual(self.logs()[-1]['config_version'], 2)

    def test_yaml_reload_routes_and_rollback(self):
        original = self.path.read_text()
        candidate = original + f'''
    - id: specific
      host: "localhost:{self.listen}"
      path_prefix: /api
      balance: round_robin
      upstreams: [node]
    - id: wildcard
      host: "*.example.test:{self.listen}"
      path_prefix: /
      balance: round_robin
      upstreams: [node]
    - id: wildcard-health
      host: "*.example.test:{self.listen}"
      path_prefix: /health
      path_match: exact
      balance: round_robin
      upstreams: [python]
    - id: regex-items
      host: "localhost:{self.listen}"
      path_pattern: '^/downloads/[a-z]+[.]zip$'
      path_match: regex
      balance: round_robin
      upstreams: [node]
'''
        def publish(contents, event):
            log = self.base / 'stderr.jsonl'
            previous = log.read_text().count(event)
            pending = self.path.with_suffix('.next')
            pending.write_text(contents)
            pending.replace(self.path)
            self.proc.send_signal(signal.SIGHUP)
            for _ in range(100):
                if log.read_text().count(event) > previous:
                    return
                time.sleep(.02)
            self.fail(f'expected {event} in stderr')

        def backend(path):
            status, response, _ = self.request(path, headers={'Host': f'LOCALHOST:{self.listen}'})
            self.assertEqual(status, 200)
            return json.loads(response)['backend']

        self.assertEqual(backend('/api/items'), 'a')
        publish(candidate, '"reloaded"')
        self.assertEqual(backend('/api/items'), 'b')
        self.assertEqual(backend('/apix'), 'a')
        self.assertEqual(backend('/downloads/item.zip?ignored=1'), 'b')
        self.assertEqual(backend('/downloads/item.ZIP'), 'a')
        status, response, _ = self.request('/', headers={'Host': f'A.B.EXAMPLE.TEST:{self.listen}'})
        self.assertEqual((status, json.loads(response)['backend']), (200, 'b'))
        for path, expected in [('/health?check=1', 'a'), ('/health/child', 'b')]:
            status, response, _ = self.request(path, headers={'Host': f'a.example.test:{self.listen}'})
            self.assertEqual((status, json.loads(response)['backend']), (200, expected))
        self.assertEqual(self.request('/', headers={'Host': f'example.test:{self.listen}'})[0], 404)
        publish(candidate.replace('*.example.test', 'a.*.test'), '"reload_rejected"')
        self.assertEqual(self.request('/', headers={'Host': f'a.example.test:{self.listen}'})[0], 200)
        publish(candidate.replace("[a-z]+", "("), '"reload_rejected"')
        self.assertEqual(backend('/downloads/item.zip'), 'b')
        publish(candidate.replace('version: 1', 'version: 2'), '"reload_rejected"')
        self.assertEqual(backend('/api/items'), 'b')
        publish(original, '"reloaded"')
        self.assertEqual(backend('/api/items'), 'a')

    def test_accepts_clients_while_reload_reads_candidate(self):
        config = self.path.read_bytes()
        pending = self.path.with_suffix('.pending')
        os.mkfifo(pending)
        pending.replace(self.path)
        self.proc.send_signal(signal.SIGHUP)
        # The pipe pauses the config read at a deterministic point. Opening the
        # writer confirms the reader has started before testing fresh sockets.
        for _ in range(100):
            try:
                writer = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                time.sleep(.02)
        else:
            self.fail('reload did not open the candidate')
        with os.fdopen(writer, 'wb') as stream:
            try:
                self.assertEqual(self.request('/ready', admin=True)[0], 200)
                self.assertEqual(self.request('/')[0], 200)
            finally:
                stream.write(config)
        self.path.unlink()
        self.path.write_bytes(config)
        for _ in range(100):
            if '"reloaded"' in (self.base / 'stderr.jsonl').read_text():
                return
            time.sleep(.02)
        self.fail('released candidate was not published')

    def test_round_robin_and_least_inflight(self):
        config = self.path.read_text().replace('upstreams = ["python"]', 'upstreams = ["python", "node"]')
        self.path.write_text(config)
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(.1)
        labels = [json.loads(self.request('/')[1])['backend'] for _ in range(4)]
        self.assertEqual(labels, ['a', 'b', 'a', 'b'])
        self.path.write_text(config.replace('round_robin', 'least_inflight'))
        self.proc.send_signal(signal.SIGHUP)
        time.sleep(.1)
        with concurrent.futures.ThreadPoolExecutor(1) as executor:
            pending = executor.submit(self.request, '/slow')
            time.sleep(.1)
            other = json.loads(self.request('/')[1])
            first = json.loads(pending.result()[1])
            self.assertNotEqual(first['backend'], other['backend'])

    def test_burst_reuse_and_eviction_during_reload(self):
        host = {'Host': f'localhost:{self.listen}'}
        start = threading.Barrier(8)

        def exchange():
            conn = http.client.HTTPConnection('127.0.0.1', self.listen, timeout=3)
            labels = set()
            try:
                for _ in range(12):
                    start.wait(timeout=3)
                    conn.request('GET', '/', headers=host)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    labels.add(json.loads(response.read())['backend'])
            finally:
                conn.close()
            return labels

        with concurrent.futures.ThreadPoolExecutor(9) as executor:
            held = executor.submit(self.request, '/held')
            try:
                self.assertTrue(self.backends[0].held.wait(timeout=2))
                self.assertEqual(json.loads(self.request('/')[1])['backend'], 'a')
                changed = self.path.read_text().replace('upstreams = ["python"]', 'upstreams = ["node"]')
                self.path.write_text(changed)
                self.proc.send_signal(signal.SIGHUP)
                for _ in range(100):
                    if '"reloaded"' in (self.base / 'stderr.jsonl').read_text():
                        break
                    time.sleep(.02)
                else:
                    self.fail('generation was not published with a live request')
                burst = [executor.submit(exchange) for _ in range(8)]
                self.assertEqual([client.result() for client in burst], [{'b'}] * 8)
            finally:
                self.backends[0].release.set()
            self.assertEqual(json.loads(held.result()[1])['backend'], 'a')

        all_events = self.logs()
        self.assertEqual(len(all_events), 98)
        self.assertEqual(sum(event['config_version'] == 1 for event in all_events), 2)
        attempts = [event['attempts'][0] for event in all_events if event['config_version'] == 2]
        self.assertGreater(sum(attempt['reused'] for attempt in attempts), 80)
        self.assertLessEqual(len({attempt['connection_id'] for attempt in attempts}), 3)

    def test_backend_unexpected_close_no_retry(self):
        self.assertEqual(self.request('/close')[0], 502)
        self.assertEqual(self.backends[0].hits, 1)
        self.assertEqual(self.request('/')[0], 503)
        time.sleep(.6)
        self.assertEqual(self.request('/')[0], 200)

    def test_body_limit_early(self):
        data = self.raw(f'POST / HTTP/1.1\r\nHost: localhost:{self.listen}\r\nContent-Length: 20000000\r\nConnection: close\r\n\r\n'.encode())
        self.assertIn(b'413', data.split(b'\r\n')[0])



    def test_unix_backend(self):
        socket_path = f'/tmp/cauce-{os.getpid()}-{self.listen}.sock'
        server = socketserver.ThreadingUnixStreamServer(socket_path, Backend)
        server.daemon_threads = True
        server.lock = threading.Lock()
        server.connections = server.hits = server.posts = 0
        server.label = 'unix'
        server.handle_error = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            config = self.path.read_text().replace(f'127.0.0.1:{self.backends[0].server_port}', f'unix:{socket_path}')
            self.path.write_text(config)
            self.proc.send_signal(signal.SIGHUP)
            time.sleep(.1)
            self.assertEqual(json.loads(self.request('/')[1])['backend'], 'unix')
            self.assertEqual(json.loads(self.request('/')[1])['connection'], 1)
            event = self.logs()[-1]
            self.assertTrue(event['attempts'][0]['reused'])
            self.assertIsNone(event['attempts'][0]['tcp_ms'])
        finally:
            server.shutdown()
            server.server_close()
            os.unlink(socket_path)

    def test_schema_and_admin_auth(self):
        self.request('/')
        c = http.client.HTTPConnection('127.0.0.1', self.admin, timeout=2)
        c.request('GET', '/metrics')
        self.assertEqual(c.getresponse().status, 403)
        c.close()
        try:
            import jsonschema
        except ImportError:
            self.skipTest('install jsonschema to validate full schema; not a pass')
        schema = json.loads((ROOT / 'schemas/access-log.schema.json').read_text())
        validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        events = self.logs()
        self.assertTrue(events)
        for event in events:
            validator.validate(event)

    def test_log_io_failure_keeps_serving(self):
        # Make the configured log parent a regular file; real open(2) failure.
        self.proc.terminate()
        self.proc.wait(timeout=3)
        logs = self.base / 'logs'
        if logs.exists():
            shutil.rmtree(logs)
        logs.write_text('not a directory')
        self.proc = subprocess.Popen([str(BIN), str(self.path)], cwd=self.base,
                                     env={**os.environ, 'CAUCE_ADMIN_TOKEN':'test-only-admin-token-at-least-32-bytes'},
                                     stderr=self.stderr, stdout=subprocess.DEVNULL)
        time.sleep(.15)
        self.assertEqual(self.request('/')[0], 200)
        time.sleep(.15)
        metrics = self.request('/metrics', admin=True)[1].decode()
        self.assertRegex(metrics, r'cauce_log_io_errors_total [1-9]')
        self.assertEqual(self.request('/')[0], 200)



    def test_pool_capacity_wait_is_bounded(self):
        with concurrent.futures.ThreadPoolExecutor(1) as executor:
            pending = executor.submit(self.request, '/slow')
            time.sleep(.1)
            start = time.monotonic()
            response = self.request('/')
            self.assertEqual(response[0], 503)
            self.assertEqual(response[1], b'pool_capacity_timeout')
            self.assertGreater(time.monotonic() - start, .2)
            self.assertLess(time.monotonic() - start, .6)
            self.assertEqual(pending.result()[0], 200)
        self.assertEqual(self.backends[0].connections, 1)

    def test_idle_and_max_age_retirement(self):
        one = json.loads(self.request('/')[1])['connection']
        time.sleep(.15)
        two = json.loads(self.request('/')[1])['connection']
        self.assertNotEqual(one, two)
        # Keep touching below idle timeout; total age still forces retirement.
        ids = []
        for _ in range(6):
            ids.append(json.loads(self.request('/')[1])['connection'])
            time.sleep(.06)
        self.assertGreater(len(set(ids)), 1)

    def test_log_rotation_and_retention(self):
        for _ in range(30):
            self.assertEqual(self.request('/')[0], 200)
        time.sleep(.2)
        files = list((self.base / 'logs').glob('access.jsonl*'))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(p.stat().st_size <= 4096 for p in files))
        for path in files:
            for line in path.read_text().splitlines():
                self.assertEqual(json.loads(line)['schema_version'], '1.1')



    def test_upload_backpressure_and_early_response_discard(self):
        with socket.create_connection(('127.0.0.1', self.listen), timeout=2) as client:
            client.sendall(f'POST / HTTP/1.1\r\nHost: localhost:{self.listen}\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n'.encode())
            self.assertTrue(self.backends[0].upload_progress.wait(timeout=.2), 'upload was buffered until completion')
            client.sendall(b'0\r\n\r\n')
            data = client.recv(8192)
            self.assertIn(b'200', data.split(b'\r\n')[0])
        # Backend deliberately responds before a declared upload finishes.
        with socket.create_connection(('127.0.0.1', self.listen), timeout=2) as client:
            client.sendall(f'POST /early HTTP/1.1\r\nHost: localhost:{self.listen}\r\nContent-Length: 1000000\r\n\r\nx'.encode())
            data = client.recv(8192)
            self.assertIn(b'200', data.split(b'\r\n')[0])
        time.sleep(.05)
        response = json.loads(self.request('/')[1])
        self.assertGreaterEqual(response['connection'], 2)



    def test_bad_upload_does_not_withdraw_healthy_backend(self):
        host = f'Host: localhost:{self.listen}\r\n'.encode()
        slow = self.raw(b'POST / HTTP/1.1\r\n' + host + b'Content-Length: 5\r\n\r\nx', wait=.4)
        self.assertIn(b'408', slow.split(b'\r\n')[0])
        # A bad client must not trip the passive upstream health policy.
        self.assertEqual(self.request('/', method='POST', body=b'ok')[0], 200)
        oversized = self.raw(b'POST / HTTP/1.1\r\n' + host + b'Transfer-Encoding: chunked\r\n\r\na\r\n0123456789\r\n0\r\n\r\n')
        self.assertIn(b'413', oversized.split(b'\r\n')[0])
        self.assertEqual(self.request('/', method='POST', body=b'ok')[0], 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
