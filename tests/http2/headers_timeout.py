#!/usr/bin/env python3
"""Raw HTTP/2 frame tests for the absolute client HEADERS deadline."""
import json
import os
import socket
import ssl
import time

import h2.config
import h2.connection
import h2.events
from hpack import Encoder

TIMEOUT_MS = 800
HOST = None


def frame(length, kind, flags, stream_id):
    return length.to_bytes(3, "big") + bytes((kind, flags)) + (stream_id & 0x7fffffff).to_bytes(4, "big")


def parse_frames(data):
    frames = []
    offset = 0
    while offset + 9 <= len(data):
        length = int.from_bytes(data[offset:offset + 3], "big")
        end = offset + 9 + length
        if end > len(data):
            break
        frames.append((data[offset + 3], data[offset + 4], int.from_bytes(data[offset + 5:offset + 9], "big") & 0x7fffffff,
                       data[offset + 9:end], data[offset:end]))
        offset = end
    return frames, data[offset:]


class Client:
    def __init__(self, tls, host=HOST):
        host = host or ("proxy-tls" if tls else "proxy-clear")
        raw = socket.create_connection((host, 8080), timeout=3)
        if tls:
            ctx = ssl.create_default_context(cafile=os.environ.get("CA_FILE", "/tls/ca.crt"))
            ctx.set_alpn_protocols(["h2"])
            raw = ctx.wrap_socket(raw, server_hostname="localhost")
            assert raw.selected_alpn_protocol() == "h2", "TLS listener did not negotiate h2"
        self.sock = raw
        self.tls = tls
        self.sock.settimeout(0.1)
        self.h2 = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
        self.h2.initiate_connection()
        self.sock.sendall(self.h2.data_to_send())
        self._wait_settings()

    def _wait_settings(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            if not data:
                raise AssertionError("server closed before SETTINGS")
            self.h2.receive_data(data)
            outbound = self.h2.data_to_send()
            if outbound:
                self.sock.sendall(outbound)
            if self.h2.remote_settings.max_concurrent_streams is not None:
                return
        raise AssertionError("timed out waiting for server SETTINGS")

    def send(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def request(self, stream_id, path="/", end_stream=True):
        self.h2.send_headers(stream_id, [(':method', 'GET'), (':scheme', 'https' if self.tls else 'http'),
                                         (':authority', 'proxy-tls:8080' if self.tls else 'proxy-clear:8080'),
                                         (':path', path)], end_stream=end_stream)
        raw = self.h2.data_to_send()
        frames, rest = parse_frames(raw)
        assert not rest and len(frames) == 1 and frames[0][0] == 1, f"expected one HEADERS frame, got {frames}"
        return frames[0]

    def wait_response(self, stream_id, timeout=4):
        deadline = time.monotonic() + timeout
        body = bytearray()
        status = None
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            if not data:
                raise AssertionError(f"connection closed before stream {stream_id} response")
            events = self.h2.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream_id:
                    status = dict(event.headers).get(":status")
                if isinstance(event, h2.events.DataReceived) and event.stream_id == stream_id:
                    body.extend(event.data)
                    self.h2.acknowledge_received_data(event.flow_controlled_length, stream_id)
            outbound = self.h2.data_to_send()
            if outbound:
                self.sock.sendall(outbound)
            if status is not None and any(isinstance(e, h2.events.StreamEnded) and e.stream_id == stream_id for e in events):
                assert status == "200", f"unexpected HTTP status {status}, body={bytes(body)!r}"
                return bytes(body)
        raise AssertionError(f"stream {stream_id} response timed out")

    def wait_response_headers(self, stream_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            if not data:
                raise AssertionError(f"connection closed before stream {stream_id} response headers")
            events = self.h2.receive_data(data)
            assert not any(isinstance(e, (h2.events.DataReceived, h2.events.StreamEnded, h2.events.StreamReset))
                           and e.stream_id == stream_id for e in events), "sibling body was not still pending"
            for event in events:
                if isinstance(event, h2.events.DataReceived):
                    self.h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                if isinstance(event, h2.events.ResponseReceived) and event.stream_id == stream_id:
                    response = dict(event.headers)
                    outbound = self.h2.data_to_send()
                    if outbound:
                        self.sock.sendall(outbound)
                    return response
            outbound = self.h2.data_to_send()
            if outbound:
                self.sock.sendall(outbound)
        raise AssertionError(f"stream {stream_id} response headers timed out")

    def healthy_request(self, stream_id):
        self.sock.sendall(self.h2.data_to_send())
        self.wait_response(stream_id)


def hpack_headers(client, stream_id, headers, end_stream=True):
    """Return h2-library-produced HEADERS frame while keeping client HPACK state."""
    client.h2.send_headers(stream_id, headers, end_stream=end_stream)
    raw = client.h2.data_to_send()
    frames, rest = parse_frames(raw)
    assert not rest and len(frames) == 1 and frames[0][0] == 1
    return frames[0]


def split_headers(item, split_at=None):
    kind, flags, sid, payload, _wire = item
    assert kind == 1 and len(payload) >= 2
    split_at = split_at or max(1, len(payload) // 2)
    first = frame(split_at, 1, flags & ~0x4, sid) + payload[:split_at]
    second = frame(len(payload) - split_at, 9, 0x4, sid) + payload[split_at:]
    return first, second


def raw_block(client, stream_id=1, headers=None):
    headers = headers or [(':method', 'GET'), (':scheme', 'https' if client.tls else 'http'),
                          (':authority', 'proxy-tls:8080' if client.tls else 'proxy-clear:8080'), (':path', '/')]
    encoded = Encoder().encode(headers)
    return encoded


def expect_closed(client, label, started, cap=1.25, minimum=0.70):
    deadline = started + cap
    while time.monotonic() < deadline:
        try:
            data = client.sock.recv(65535)
            if not data:
                elapsed = time.monotonic() - started
                if elapsed < minimum:
                    raise AssertionError(f"{label}: connection closed too early ({elapsed:.3f}s)")
                return elapsed
            # A GOAWAY alone is not enough; wait for the connection to close.
        except (socket.timeout, ssl.SSLWantReadError):
            continue
        except (ConnectionResetError, BrokenPipeError, ssl.SSLError, OSError):
            elapsed = time.monotonic() - started
            if elapsed < minimum:
                raise AssertionError(f"{label}: connection closed too early ({elapsed:.3f}s)")
            return elapsed
    raise AssertionError(f"{label}: incomplete HEADERS did not close connection within {cap}s")


def begin_incomplete(client, payload=None, end_headers=True, stream_id=1, truncate_frame_payload=False):
    payload = payload or raw_block(client)
    split = max(1, len(payload) // 2)
    flags = (0x1 | (0x4 if end_headers else 0))  # END_STREAM, optionally END_HEADERS
    declared_length = len(payload) if truncate_frame_payload else split
    client.send(frame(declared_length, 1, flags, stream_id) + payload[:split])
    return payload[split:]


def case_incomplete_payload(tls):
    c = Client(tls)
    try:
        started = time.monotonic()
        begin_incomplete(c, end_headers=True, truncate_frame_payload=True)
        elapsed = expect_closed(c, "truncated HEADERS payload", started)
        # The timer starts at the first HEADERS frame header byte. Account for setup and send.
        return f"incomplete HEADERS payload (END_HEADERS set), {'TLS' if tls else 'clear'}: closed in {elapsed:.3f}s"
    finally:
        c.close()


def case_missing_end_headers(tls):
    c = Client(tls)
    try:
        started = time.monotonic()
        begin_incomplete(c, end_headers=False)
        elapsed = expect_closed(c, "missing END_HEADERS", started)
        return f"HEADERS awaits CONTINUATION, {'TLS' if tls else 'clear'}: closed in {elapsed:.3f}s"
    finally:
        c.close()


def case_first_frame_header_byte(tls):
    c = Client(tls)
    try:
        started = time.monotonic()
        c.send(b"\x00")  # first byte of a HEADERS frame header starts the deadline
        elapsed = expect_closed(c, "single initial HEADERS frame-header byte", started)
        return f"one-byte HEADERS frame header, {'TLS' if tls else 'clear'}: closed in {elapsed:.3f}s"
    finally:
        c.close()


def case_headers_frame_header_drip(tls):
    c = Client(tls)
    try:
        encoded = raw_block(c)
        wire = frame(len(encoded), 1, 0x5, 1) + encoded
        started = time.monotonic()
        c.send(wire[:1])
        time.sleep(0.32)
        try:
            c.send(wire[1:4])
        except OSError:
            pass
        time.sleep(0.32)
        try:
            c.send(wire[4:8])
        except OSError:
            pass
        elapsed = expect_closed(c, "partial initial HEADERS frame-header drip", started)
        return f"dripped initial HEADERS frame header, {'TLS' if tls else 'clear'}: closed in {elapsed:.3f}s"
    finally:
        c.close()


def case_continuation_drip(tls, payload_drip):
    c = Client(tls)
    try:
        started = time.monotonic()
        tail = begin_incomplete(c, end_headers=False)
        sid = 1
        if payload_drip:
            block = tail
            continuation = frame(len(block), 9, 0x4, sid)
            try:
                c.send(continuation + block[:1])
            except OSError:
                pass
            time.sleep(0.32)
            try:
                c.send(block[1:2])
            except OSError:
                pass
            time.sleep(0.32)
            try:
                c.send(block[2:3])
            except OSError:
                pass
            label = "partial CONTINUATION payload drip"
        else:
            try:
                c.send(frame(48, 9, 0, sid)[:2])
            except OSError:
                pass
            time.sleep(0.32)
            try:
                c.send(frame(48, 9, 0, sid)[2:5])
            except OSError:
                pass
            time.sleep(0.32)
            try:
                c.send(frame(48, 9, 0, sid)[5:8])
            except OSError:
                pass
            label = "partial CONTINUATION frame-header drip"
        elapsed = expect_closed(c, label, started)
        return f"{label}, {'TLS' if tls else 'clear'}: closed in {elapsed:.3f}s"
    finally:
        c.close()


def case_trailer_block(tls):
    c = Client(tls)
    try:
        enc = Encoder()
        initial = enc.encode([(':method', 'POST'), (':scheme', 'https' if tls else 'http'),
                              (':authority', 'proxy-tls:8080' if tls else 'proxy-clear:8080'),
                              (':path', '/'), ('content-length', '1')])
        c.send(frame(len(initial), 1, 0x4, 1) + initial)
        c.send(frame(1, 0, 0, 1) + b'x')
        trailers = enc.encode([('x-trailer-check', 'yes')])
        split = max(1, len(trailers) // 2)
        started = time.monotonic()
        c.send(frame(len(trailers), 1, 0x5, 1) + trailers[:split])  # partial terminal trailer frame payload
        elapsed = expect_closed(c, "incomplete trailing HEADERS", started)
        return f"incomplete trailers, {'TLS' if tls else 'clear'}: closed in {elapsed:.3f}s"
    finally:
        c.close()


def case_valid_fragmented_reuse(tls):
    c = Client(tls)
    try:
        first = hpack_headers(c, 1, [(':method', 'GET'), (':scheme', 'https' if tls else 'http'),
                                     (':authority', 'proxy-tls:8080' if tls else 'proxy-clear:8080'),
                                     (':path', '/')], True)
        part1, part2 = split_headers(first)
        c.send(part1)
        time.sleep(0.12)
        c.send(part2)
        c.wait_response(1)
        time.sleep(0.95)  # exceeds the 800ms header budget after block completion
        c.h2.send_headers(3, [(':method', 'GET'), (':scheme', 'https' if tls else 'http'),
                              (':authority', 'proxy-tls:8080' if tls else 'proxy-clear:8080'), (':path', '/')], end_stream=True)
        c.sock.sendall(c.h2.data_to_send())
        c.wait_response(3)
        return f"valid fragmented HEADERS then idle/reuse, {'TLS' if tls else 'clear'}: streams 1 and 3 returned 200"
    finally:
        c.close()


def case_body_not_timed_out(tls):
    c = Client(tls)
    try:
        c.h2.send_headers(1, [(':method', 'POST'), (':scheme', 'https' if tls else 'http'),
                              (':authority', 'proxy-tls:8080' if tls else 'proxy-clear:8080'),
                              (':path', '/'), ('content-length', '4')], end_stream=False)
        c.sock.sendall(c.h2.data_to_send())
        c.h2.send_data(1, b'abcd', end_stream=True)
        datawire = c.h2.data_to_send()
        frames, rest = parse_frames(datawire)
        assert not rest and len(frames) == 1 and frames[0][0] == 0, "expected one complete DATA frame"
        wire = frames[0][4]
        c.sock.sendall(wire[:10])  # complete DATA frame header and one body byte
        time.sleep(0.30)
        c.sock.sendall(wire[10:11])
        time.sleep(0.30)
        c.sock.sendall(wire[11:12])
        time.sleep(0.30)
        c.sock.sendall(wire[12:])
        c.wait_response(1)
        return f"slow DATA body exceeds HEADERS budget, {'TLS' if tls else 'clear'}: request returned 200"
    finally:
        c.close()


def case_connection_sibling(tls):
    c = Client(tls)
    try:
        trace = '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01'
        c.h2.send_headers(1, [(':method', 'GET'), (':scheme', 'https' if tls else 'http'),
                              (':authority', 'proxy-tls:8080' if tls else 'proxy-clear:8080'),
                              (':path', '/lifecycle'), ('traceparent', trace)], end_stream=True)
        c.sock.sendall(c.h2.data_to_send())
        response_headers = c.wait_response_headers(1)
        assert response_headers.get(':status') == '200' and response_headers.get('x-request-id'), response_headers
        # The fixture sends response headers immediately and holds the body open
        # for 1.2s, so this stream remains active as the client-header timer fires.
        started = time.monotonic()
        begin_incomplete(c, end_headers=False, stream_id=3)
        elapsed = expect_closed(c, "incomplete block connection scope", started)
        fresh = Client(tls)
        try:
            fresh.h2.send_headers(1, [(':method', 'GET'), (':scheme', 'https' if tls else 'http'),
                                      (':authority', 'proxy-tls:8080' if tls else 'proxy-clear:8080'), (':path', '/')], end_stream=True)
            fresh.sock.sendall(fresh.h2.data_to_send())
            fresh.wait_response(1)
        finally:
            fresh.close()
        return (f"stalled connection cancels active response-body sibling; fresh connection succeeds, {'TLS' if tls else 'clear'}: {elapsed:.3f}s",
                response_headers['x-request-id'])
    finally:
        c.close()


def main():
    checks = []
    sibling_request_ids = {}
    started = time.monotonic()
    for tls in (False, True):
        checks.append(case_incomplete_payload(tls))
        checks.append(case_first_frame_header_byte(tls))
        checks.append(case_headers_frame_header_drip(tls))
        checks.append(case_missing_end_headers(tls))
        checks.append(case_continuation_drip(tls, False))
        checks.append(case_continuation_drip(tls, True))
        checks.append(case_trailer_block(tls))
        checks.append(case_valid_fragmented_reuse(tls))
        checks.append(case_body_not_timed_out(tls))
        check, request_id = case_connection_sibling(tls)
        checks.append(check)
        sibling_request_ids['proxy-tls' if tls else 'proxy-clear'] = request_id
    print(json.dumps({"result": "PASS", "sibling_request_ids": sibling_request_ids, "timeout_ms": TIMEOUT_MS, "elapsed_seconds": round(time.monotonic() - started, 3), "checks": checks}))


if __name__ == "__main__":
    main()
