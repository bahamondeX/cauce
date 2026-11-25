#!/usr/bin/env python3
"""Profile the existing grpcio echo fixture with its message-validating Node client."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from capacity import digest, write_json
from telemetry import H2_LAB, available_port, http, process, until


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy", type=Path)
    parser.add_argument("certificates", type=Path, help="existing Increment 8 fixture certificates")
    parser.add_argument("output", type=Path)
    parser.add_argument("--method", choices=("Unary", "Bidi"), required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--cpus", type=int, nargs=3, default=[0, 1, 2])
    parser.add_argument("--seconds", type=int, default=12)
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--perf", default="perf")
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--flamegraph", type=Path, required=True)
    args = parser.parse_args()
    if (sys.platform != "linux" or len(set(args.cpus)) != 3
            or not set(args.cpus).issubset(os.sched_getaffinity(0))
            or args.seconds < 3 or args.rate < 1):
        parser.error("use three available Linux CPU indices and positive workload settings")
    root = args.output.resolve()
    root.mkdir(parents=True)
    listen, admin, grpc_port, stats_port = (available_port() for _ in range(4))
    config = root / "proxy.toml"
    source_config = (H2_LAB / "configs" / "cauce-grpc-clear.toml").read_text()
    config.write_text(source_config.replace("0.0.0.0:8080", f"127.0.0.1:{listen}")
                      .replace("127.0.0.1:9090", f"127.0.0.1:{admin}")
                      .replace("proxy:8080", f"127.0.0.1:{listen}")
                      .replace("backend-grpc:50051", f"127.0.0.1:{grpc_port}"))
    proxy = args.proxy.resolve()
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.backend_image], text=True).strip()
    client_script = H2_LAB / "load_h2.mjs"
    write_json(root / "identities.json", {
        "proxy_sha256": digest(proxy), "backend_image_id": image_id,
        "client_sha256": digest(client_script), "method": args.method, "cpus": args.cpus,
        "rate": args.rate, "profile_seconds": args.seconds,
    })
    volume = f"{args.certificates.resolve()}:/certs:ro"
    backend = subprocess.check_output([
        "docker", "run", "-d", "--pull", "never", "--cpuset-cpus", str(args.cpus[1]),
        "-p", f"127.0.0.1:{grpc_port}:50051", "-p", f"127.0.0.1:{stats_port}:9001",
        "-v", volume, image_id,
    ], text=True).strip()
    try:
        with process(["taskset", "-c", str(args.cpus[0]), str(proxy), str(config)],
                     root / "proxy.log", {**os.environ, "TOKIO_WORKER_THREADS": "1"}) as app:
            until(app, lambda: http(admin, "/ready") and http(stats_port, "/stats"), 10)
            output = root / "offers.jsonl"
            command = [
                "taskset", "-c", str(args.cpus[2]), "node", str(client_script),
                "--url", f"http://127.0.0.1:{listen}", "--mode", "grpc", "--method", args.method,
                "--rate", str(args.rate), "--seconds", str(args.seconds + 3), "--warmup", "2",
                "--out", str(output), "--barrier", str(root),
            ]
            with process(command, root / "client.log") as client:
                until(client, (root / "warmup.done").exists, 10)
                (root / "resume").touch()
                capture = [
                    sys.executable, str(Path(__file__).with_name("profile-linux.py")),
                    str(app.pid), str(root / "profile"), "--mode", "record",
                    "--seconds", str(args.seconds), "--perf", args.perf,
                    "--flamegraph", str(args.flamegraph.resolve()),
                ]
                if args.sudo:
                    capture.append("--sudo")
                profile = subprocess.run(capture, timeout=args.seconds + 35, check=False)
                until(client, (root / "measurement.done").exists, args.seconds + 10)
                (root / "release").touch()
                if client.wait(timeout=15) or profile.returncode:
                    return 1
            summary = json.loads(output.with_suffix(".summary.json").read_text())
            return int(summary["ok"] != summary["offered"] or summary["scheduler_lag_ms"]["p99"] > 10)
    finally:
        subprocess.run(["docker", "rm", "-f", backend], stdout=subprocess.DEVNULL, check=True)


if __name__ == "__main__":
    sys.exit(main())
