import concurrent.futures
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import grpc

LOCK = threading.Lock()
STATE = {"active": 0, "peak_active": 0, "calls": 0, "requests": 0, "completed": 0,
         "cancelled": 0, "connections": 0, "peak_connections": 0,
         "new_connections": 0, "peers": {}}


def enter(context, method):
    peer = context.peer()
    with LOCK:
        row = STATE["peers"].setdefault(peer, {"active": 0, "peak_active": 0, "calls": 0})
        row["active"] += 1
        row["calls"] += 1
        row["peak_active"] = max(row["peak_active"], row["active"])
        STATE["active"] += 1
        STATE["calls"] += 1
        STATE["requests"] += 1
        STATE["peak_active"] = max(STATE["peak_active"], STATE["active"])
    context.send_initial_metadata((('backend-initial', 'grpc'), ('x-rpc-method', method)))
    context.set_trailing_metadata((('backend-trailer', 'complete'),))
    return peer, row


def leave(context, peer):
    with LOCK:
        row = STATE["peers"][peer]
        row["active"] = max(0, row["active"] - 1)
        STATE["active"] = max(0, STATE["active"] - 1)
        if context.is_active():
            STATE["completed"] += 1
        else:
            STATE["cancelled"] += 1


def delay(context):
    values = dict(context.invocation_metadata())
    try:
        ms = max(0, min(30000, int(values.get("x-delay-ms", "0"))))
    except ValueError:
        ms = 0
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        if not context.is_active():
            return False
        time.sleep(max(0, min(.01, deadline - time.monotonic())))
    return context.is_active()


def unary(request, context):
    peer, _ = enter(context, "Unary")
    try:
        if not delay(context): return b""
        return b"echo:" + request
    finally:
        leave(context, peer)


def client_stream(requests, context):
    peer, _ = enter(context, "ClientStream")
    try:
        if not delay(context): return b""
        return b"|".join(requests)
    finally:
        leave(context, peer)


def server_stream(request, context):
    peer, _ = enter(context, "ServerStream")
    try:
        if not delay(context): return
        for i in range(3):
            if not context.is_active(): return
            yield request + b":" + str(i).encode()
    finally:
        leave(context, peer)


def bidi(requests, context):
    peer, _ = enter(context, "Bidi")
    try:
        if not delay(context): return
        for request in requests:
            if not context.is_active(): return
            yield b"bidi:" + request
    finally:
        leave(context, peer)


def reset():
    with LOCK:
        if STATE["active"]:
            return False
        STATE.update(active=0, peak_active=0, calls=0, requests=0, completed=0, cancelled=0,
                     peak_connections=STATE["connections"], new_connections=0,
                     peers={})
        return True


def snapshot():
    with LOCK:
        return {**STATE, "peers": {k: dict(v) for k, v in STATE["peers"].items()},
                "peer_count": len(STATE["peers"])}


def connection_sampler():
    """Track established sockets to either gRPC port; peer rows track stream RPCs."""
    previous = set()
    while True:
        current = set()
        try:
            for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    lines = Path(filename).read_text().splitlines()[1:]
                except OSError:
                    continue
                for line in lines:
                    fields = line.split()
                    local, remote, status = fields[1], fields[2], fields[3]
                    port = int(local.rsplit(":", 1)[1], 16)
                    if port in (50051, 50052) and status == "01":
                        current.add(f"{filename}:{local}>{remote}")
        except (ValueError, IndexError):
            pass
        with LOCK:
            added = current - previous
            STATE["connections"] = len(current)
            STATE["new_connections"] += len(added)
            STATE["peak_connections"] = max(STATE["peak_connections"], len(current))
        previous = current
        time.sleep(0.05)


class Stats(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/reset":
            ok = reset()
            body = json.dumps(snapshot() if ok else {"error": "active calls"}).encode()
            status = 200 if ok else 409
        elif self.path == "/stats":
            body = json.dumps(snapshot()).encode()
            status = 200
        else:
            self.send_error(404); return
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if self.path != "/reset":
            self.send_error(404); return
        ok = reset()
        body = json.dumps(snapshot() if ok else {"error": "active calls"}).encode()
        self.send_response(200 if ok else 409); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *_):
        return


def start_grpc(port, secure=False):
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=40), options=[
        ("grpc.max_concurrent_streams", 16),
        ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        ("grpc.max_send_message_length", 16 * 1024 * 1024),
    ])
    methods = {
        "Unary": grpc.unary_unary_rpc_method_handler(unary, request_deserializer=lambda x: x,
            response_serializer=lambda x: x),
        "ClientStream": grpc.stream_unary_rpc_method_handler(client_stream, request_deserializer=lambda x: x,
            response_serializer=lambda x: x),
        "ServerStream": grpc.unary_stream_rpc_method_handler(server_stream, request_deserializer=lambda x: x,
            response_serializer=lambda x: x),
        "Bidi": grpc.stream_stream_rpc_method_handler(bidi, request_deserializer=lambda x: x,
            response_serializer=lambda x: x),
    }
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("fixture.Echo", methods),))
    if secure:
        server_credentials = grpc.ssl_server_credentials(((Path('/certs/backend.key').read_bytes(),
            Path('/certs/backend.crt').read_bytes()),))
        server.add_secure_port(f"0.0.0.0:{port}", server_credentials)
    else:
        server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    return server


threading.Thread(target=connection_sampler, daemon=True).start()
SERVERS = [start_grpc(port, secure) for port, secure in ((50051, False), (50052, True))]
ThreadingHTTPServer(("0.0.0.0", 9001), Stats).serve_forever()
