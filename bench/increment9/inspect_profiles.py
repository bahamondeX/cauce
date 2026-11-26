#!/usr/bin/env python3
"""Measure retained release artifacts and real local process startup and idle RSS."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def bind_candidate():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def sample(binary, config, work, run, time_tool):
    admin = bind_candidate()
    config["admin"] = f"127.0.0.1:{admin}"
    config["listen"] = f"127.0.0.1:{bind_candidate()}"
    path = work / "proxy.yaml"
    path.write_text(json.dumps({"version": 1, "proxy": config}))
    measured = [time_tool, "-l", str(binary), str(path)]
    log = work / "process.log"
    env = {**os.environ, "TOKIO_WORKER_THREADS": "1"}
    with log.open("wb") as output:
        start = time.perf_counter_ns()
        process = subprocess.Popen(measured, cwd=work, stderr=output, env=env)
    gateway = None
    try:
        while True:
            assert process.poll() is None, log.read_text()
            probe = http.client.HTTPConnection("127.0.0.1", admin, timeout=0.25)
            try:
                probe.request("GET", "/ready")
                response = probe.getresponse()
                if response.status == 200:
                    startup_us = (time.perf_counter_ns() - start) / 1000
                    break
            except OSError:
                pass
            finally:
                probe.close()
            if time.perf_counter_ns() - start > 5_000_000_000:
                raise TimeoutError("readiness")
            time.sleep(0.001)
        gateway = int(subprocess.check_output(["pgrep", "-P", str(process.pid)], text=True).strip())
        time.sleep(0.1)
        rss = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(gateway)], text=True))
    finally:
        if gateway:
            os.kill(gateway, 15)
        else:
            process.terminate()
        process.wait(timeout=8)
    raw_time = log.read_text()
    metrics = {}
    for line in raw_time.splitlines():
        fields = line.split()
        if fields and fields[0].isdigit():
            field = " ".join(fields[1:])
            if field in {"peak memory footprint", "maximum resident set size", "page reclaims", "page faults"}:
                metrics[field] = int(fields[0])
    return {"sample": run, "startup_us": startup_us, "idle_rss_kib": rss, "time_l": metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--full", required=True, type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    config = tomllib.loads((ROOT / "examples/cauce.toml").read_text())
    config["logs"]["enabled"] = False
    config["upstreams"] = []
    config["routes"] = [{"id": "ready", "host": "localhost", "path_prefix": "/",
                         "content": {"type": "direct", "status": 200, "body": "ready"}}]
    full_web = json.loads(json.dumps(config))
    full_web["routes"][0]["compression"] = {"algorithms": ["gzip"]}
    variants = [("full-disabled", args.full.resolve(), config),
                ("full-enabled", args.full.resolve(), full_web),
                ("core", args.core.resolve(), config)]
    print(json.dumps({"source": args.source, "system": platform.platform(), "workers": 1,
                      "workload": "startup, one successful readiness request, and 100 ms idle",
                      "enabled_feature": "response compression configured on a direct route"}))
    seen = set()
    for index in range(args.runs):
        for name, binary, data in (variants if index % 2 == 0 else reversed(variants)):
            if name not in seen:
                seen.add(name)
                print(json.dumps({"profile": name, "binary_bytes": binary.stat().st_size,
                                  "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                                  "capabilities": json.loads(subprocess.check_output([binary, "capabilities", "--json"]))}))
            with tempfile.TemporaryDirectory() as folder:
                values = sample(binary, data, Path(folder), index, "/usr/bin/time")
                print(json.dumps({"profile": name, **values}))


if __name__ == "__main__":
    main()
