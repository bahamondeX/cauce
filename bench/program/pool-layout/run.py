#!/usr/bin/env python3
"""Randomized paired study of the idle-store prototypes."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cpus", type=int, nargs="+", required=True)
    parser.add_argument("--reps", type=int, default=7)
    parser.add_argument("--ops", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()
    if (platform.system() != "Linux" or not set(args.cpus) <= os.sched_getaffinity(0)
            or len(set(args.cpus)) != len(args.cpus) or args.reps < 7 or args.ops < 128):
        parser.error("use disjoint Linux CPUs, at least seven paired repetitions and 128 operations")
    os.sched_setaffinity(0, args.cpus)
    args.output.mkdir(parents=True)
    (args.output / "plan.json").write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kernel": platform.release(), "machine": platform.machine(), "cpus": args.cpus,
        "seed": args.seed, "pairs": args.reps, "ops_per_worker": args.ops,
        "binary_sha256": hashlib.file_digest(args.binary.open("rb"), "sha256").hexdigest(),
        "acceptance": "upper paired time delta <= -10% with no accounting failure; storage prototype scope",
    }, indent=2) + "\n")
    rng = random.Random(args.seed)
    raw = args.output / "raw.jsonl"
    with raw.open("w") as rows:
        for repetition in range(args.reps):
            for endpoints, mode in ((1, "stable"), (64, "stable"), (64, "churn")):
                order = list("ABC")
                rng.shuffle(order)
                for variant in order:
                    data = subprocess.check_output([str(args.binary.resolve()), variant,
                                                   str(len(args.cpus)), str(endpoints), str(args.ops), mode],
                                                   text=True)
                    row = json.loads(data)
                    rows.write(json.dumps({**row, "repetition": repetition}) + "\n")
                    rows.flush()

    cells = {}
    for line in raw.read_text().splitlines():
        row = json.loads(line)
        key = row["endpoints"], row["workload"], row["repetition"], row["variant"]
        if (key in cells or key[3] not in "ABC" or row["ops"] != len(args.cpus) * args.ops
                or row["reused"] + row["opened_tokens"] != row["ops"]
                or row["opened_tokens"] - row["dropped"] - row["denied"] != row["idle"]):
            raise ValueError(f"invalid cell {key}")
        cells[key] = row

    bootstrap_rng = random.Random(args.seed + 1)
    results = {}
    for endpoints, mode in sorted({key[:2] for key in cells}):
        results[f"{endpoints}-{mode}"] = result = {}
        baseline = [cells[endpoints, mode, rep, "A"]["elapsed_ns"] for rep in range(args.reps)]
        for variant in "ABC":
            sample = [cells[endpoints, mode, rep, variant] for rep in range(args.reps)]
            deltas = [100 * (b["elapsed_ns"] / a - 1) for a, b in zip(baseline, sample)]
            estimates = sorted(statistics.median(bootstrap_rng.choices(deltas, k=len(deltas)))
                               for _ in range(10_000))
            result[variant] = {
                "paired_time_delta_percent": statistics.median(deltas),
                "paired_time_delta_ci95_percent": [estimates[250], estimates[9749]],
                **{f"median_{key}": statistics.median(cell[key] for cell in sample) for key in (
                    "elapsed_ns", "lock_wait_p99_ns", "lock_hold_p99_ns", "opened_tokens",
                    "peer_reuse", "worker_reuse_min", "worker_reuse_max", "dropped", "denied"
                )},
            }
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
