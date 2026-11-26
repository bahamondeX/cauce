#!/usr/bin/env python3
"""Record paired loopback timings for nested location and header options."""

import argparse
import http.client
import json
from pathlib import Path
import runpy
import statistics
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[2]
support = runpy.run_path(str(ROOT / "scripts/nginx-compatibility.py"))


def measure(binary, variant, count, folder):
    port, admin = support["available_ports"](2)
    configuration = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
    parent = {"id": "parent", "host": "compat.test", "path_prefix": "/api", "path_match": "literal_prefix",
              "content": {"type": "direct", "status": 200, "body": "same"}}
    leaf = {**parent, "id": "leaf", "path_prefix": "/api/child", "path_match": "exact"}
    if variant == "nested-location":
        leaf["parent"] = "parent"
    if variant == "response-addition":
        leaf["headers"] = {"response_add": [{"name": "X-Fixture", "value": "enabled"}]}
    configuration.update(listen=f"127.0.0.1:{port}", admin=f"127.0.0.1:{admin}",
                         upstreams=[], routes=[parent, leaf])
    configuration["logs"]["enabled"] = False
    path = folder / "config.yaml"
    path.write_text(json.dumps({"version": 1, "proxy": configuration}))
    observations = []
    with support["launch"](binary, [path], port, folder) as (process, lifecycle):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            before = support["resource_sample"](process)
            for index in range(count + 64):
                start = time.perf_counter_ns()
                connection.request("GET", "/api/child", headers={"Host": "compat.test"})
                response = connection.getresponse()
                if response.status != 200 or response.read() != b"same":
                    raise RuntimeError("request failed the direct-response fixture")
                expected = "enabled" if variant == "response-addition" else None
                if response.getheader("X-Fixture") != expected:
                    raise RuntimeError("unexpected response header")
                if index >= 64:
                    observations.append(time.perf_counter_ns() - start)
        finally:
            connection.close()
        after = support["resource_sample"](process)
    if lifecycle["forced_kill"] or lifecycle["exit_status"]:
        raise RuntimeError("Cauce failed to stop cleanly")
    samples = sorted(observations)
    return {"variant": variant, "requests": count, "p50_ns": statistics.median(samples),
            "p99_ns": samples[(99 * len(samples) + 99) // 100 - 1],
            "before": before, "after": after, "samples_ns": observations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cauce", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--requests", type=int, default=1024, choices=range(64, 65537), metavar="64..65536")
    parser.add_argument("--work", type=support["local_output"], default=ROOT / "work/compatibility-overhead")
    args = parser.parse_args()
    binary = args.cauce.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"source": args.source, "cauce_sha256": support["digest"](binary),
                      "scope": "single-client persistent HTTP/1 loopback diagnostic"}))
    variants = ["features-disabled", "nested-location", "response-addition"]
    for repeat in range(3):
        for variant in variants[repeat:] + variants[:repeat]:
            with tempfile.TemporaryDirectory(prefix="cauce-overhead-", dir=args.work) as folder:
                result = measure(binary, variant, args.requests, Path(folder))
            print(json.dumps({"repeat": repeat, **result}))


if __name__ == "__main__":
    main()
