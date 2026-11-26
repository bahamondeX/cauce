#!/usr/bin/env python3
"""Run prioritized native HTTP/1.1 cells from staged, architecture-matched artifacts."""

import argparse
import itertools
import json
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys

from capacity import digest, write_json
PROGRAM = Path(__file__).resolve().parent
RATE = 1000
SEED = 810333


def paired(cells):
    good = [cell["metrics"] for cell in cells if cell["qualified"]]
    rng = random.Random(SEED)
    report = {"qualified_pairs": len(good), "runs": cells, "practical_improvement": 0.05}
    if len(good) < 7:
        return report
    report["metrics"] = {}
    for metric in good[0]["cauce"]:
        left = [item["cauce"][metric] for item in good]
        right = [item["nginx"][metric] for item in good]
        if any(item is None for item in left + right):
            continue
        differences = [a - b for a, b in zip(left, right)]
        boot = sorted(statistics.median(rng.choices(differences, k=len(good))) for _ in range(10000))
        report["metrics"][metric] = {
            "cauce_median": statistics.median(left), "nginx_median": statistics.median(right),
            "median_paired_difference": statistics.median(differences),
            "difference_ci95": [boot[250], boot[9749]],
        }
    return report


def metrics(data):
    target = data["target"]
    backend = data["backend_before"], data["backend_after"]
    return {
        "cpu_us_per_success": target["cpu_us_per_success"],
        "rss_peak_bytes": target["process"]["rss_peak_sampled_bytes"],
        "pss_end_bytes": target["process"]["after"]["pss_bytes"],
        "reused_requests": backend[1]["reused_requests"] - backend[0]["reused_requests"],
        **{name + "_ns": value for name, value in target["scheduled_latency_ns"].items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bin", type=Path, help="staged directory with cauce, nginx, and cauce-load")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.bin, args.output = args.bin.resolve(), args.output.resolve()
    args.output.mkdir(parents=True)
    cpus = set(range(12))
    if sys.platform != "linux" or not cpus <= os.sched_getaffinity(0):
        parser.error("needs twelve available Linux CPUs in the staged manifest layout")
    generator = args.bin / "cauce-load"
    paths = {name: args.bin / name for name in ("cauce", "nginx")}
    output = args.output
    references = [name for name in ("haproxy", "pingora") if (args.bin / name).is_file()]
    revisions = [name for name in ("cauce-published", "cauce-checkpoint") if (args.bin / name).is_file()]
    identities = {name: digest(args.bin / name) for name in [*paths, *references, *revisions]}
    identities["generator"] = digest(generator)
    pool_binary = "cauce-fixed" if (args.bin / "cauce-fixed").is_file() else "cauce"
    identities["pool_binary"] = digest(args.bin / pool_binary)
    write_json(output / "identities.json", identities)
    rng = random.Random(SEED)
    results = {}

    def run(label, name, workers=1, *, protocol="http1", rates=(500, 1000, 2000, 4000, 8000, 16000),
            matched=False, endpoints=1, strategy="compact", seconds=5, stability=0):
        dest = output / label
        command = [
            sys.executable, str(PROGRAM / "capacity-workload.py"), str(paths.get(name, args.bin / name)),
            str(generator), str(dest), "--proxy-kind", name if name in ("nginx", *references) else "cauce",
            "--protocol", protocol, "--strategy", strategy, "--endpoints", str(endpoints),
            "--workers", str(workers), "--proxy-cpus", *map(str, range(workers)),
            "--backend-cpu", "10", "--client-cpu", "8", "9", "--sampler-cpu", "11",
            "--p99-ms", "20", "--p999-ms", "100", "--seconds", str(seconds), "--warmup", "2",
            "--confirm-seconds", "60", "--refine-steps", "3", "--stability-seconds", str(stability),
            "--rates", *map(str, rates),
        ]
        if matched:
            command.append("--matched")
        with (output / (label + ".log")).open("w") as stream:
            rc = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False).returncode
        results[label] = rc
        write_json(output / "exit-status.json", results)
        print(label, rc, flush=True)
        path = dest / ("matched.json" if matched else "measurements/campaign.json")
        return json.loads(path.read_text()) if path.is_file() else {}

    order = list(paths)
    rng.shuffle(order)
    for name in order:
        primary = run(f"{name}-1w", name)
        if primary.get("status") in ("REJECTED_POSTFLIGHT", "CONFIRMATION_FAILED"):
            bound = primary.get("highest_passing_ramp")
            lower_rates = tuple(rate for rate in (500, 1000, 2000, 4000, 8000)
                                if bound and rate <= bound // 2)
            if lower_rates:
                run(f"{name}-1w-conservative", name, rates=lower_rates)
    pairs = []
    for repeat in range(1, 8):
        rng.shuffle(order)
        data = {name: run(f"pair-{repeat}-{name}", name, rates=(RATE,), matched=True) for name in order}
        qualified = all(run.get("qualified") for run in data.values())
        cell = {"repeat": repeat, "order": order.copy(), "qualified": qualified}
        if qualified:
            cell["metrics"] = {name: metrics(report) for name, report in data.items()}
        pairs.append(cell)
        write_json(output / "paired.json", paired(pairs))

    for workers in (2, 4, 8):
        primary = run(f"cauce-{workers}w", "cauce", workers, stability=90 if workers == 4 else 0)
        if primary.get("status") in ("REJECTED_POSTFLIGHT", "CONFIRMATION_FAILED", "STABILITY_FAILED"):
            bound = primary.get("highest_passing_ramp")
            lower_rates = tuple(rate for rate in (500, 1000, 2000, 4000, 8000)
                                if bound and rate <= bound // 2)
            if lower_rates:
                run(f"cauce-{workers}w-conservative", "cauce", workers,
                    rates=lower_rates, stability=90 if workers == 4 else 0)
    for name in revisions:
        run(f"{name.removeprefix('cauce-')}-1w", name, rates=(500, 1000, 2000, 4000, 8000))
    for name in references:
        run(f"{name}-1w", name, rates=(500, 1000, 2000, 4000, 8000))
    run("cauce-h2-1w", "cauce", protocol="http2", rates=(500, 1000, 2000, 4000, 8000))
    for strategy, endpoints in itertools.product(("compact", "sharded"), (1, 4, 16, 64, 256)):
        run(f"pool-{strategy}-{endpoints}", pool_binary, 4, rates=(RATE,), matched=True,
            strategy=strategy, endpoints=endpoints)


if __name__ == "__main__":
    main()
