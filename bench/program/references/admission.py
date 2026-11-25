#!/usr/bin/env python3
"""Observe the 33rd concurrent request and post-overload behavior."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import sys
import time


def request(host, port, path):
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def main():
    stack, = sys.argv[1:]
    expected_excess = {"pingora": 503, "haproxy": 200}[stack]
    assert request("backend", 9000, "/reset")[0] == 200
    with ThreadPoolExecutor(max_workers=32) as executor:
        active = [executor.submit(request, stack, 8080, "/echo?ms=750") for _ in range(32)]
        until = time.monotonic() + .5
        while time.monotonic() < until:
            state = json.loads(request("backend", 9000, "/stats")[1])
            if state["active"] == 32:
                break
            time.sleep(.01)
        else:
            raise AssertionError("never held 32 active requests upstream")
        start = time.monotonic()
        excess, body = request(stack, 8080, "/echo")
        elapsed = time.monotonic() - start
        assert excess == expected_excess, (stack, excess, body)
        if excess == 200:
            assert body == b"x" * 1024
        for work in active:
            assert work.result() == (200, b"x" * 1024)
    assert request(stack, 8080, "/echo") == (200, b"x" * 1024)
    final = json.loads(request("backend", 9000, "/stats")[1])
    assert final["peak_active"] == 32 and final["active"] == 0
    print(json.dumps({"stack": stack, "held": 32, "peak_backend_active": final["peak_active"],
                      "excess_status": excess, "excess_seconds": elapsed,
                      "backend_requests": final["requests"], "recovered": True}))


if __name__ == "__main__":
    main()
