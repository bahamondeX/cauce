#!/usr/bin/env python3
"""Summarize paired route-lookup repetitions and a bootstrap interval."""

import argparse
from collections import defaultdict
import csv
import random
import statistics


def read_samples(path):
    groups = defaultdict(dict)
    with open(path, newline="") as records:
        for row in csv.DictReader(records):
            key = row["layout"], int(row["routes"])
            run = int(row["repeat"]), row["matcher"]
            if run in groups[key] or run[1] not in {"linear", "radix"}:
                raise ValueError(f"Invalid run {key} {run}")
            groups[key][run] = int(row["elapsed_ns"]) / int(row["lookups"])
    if not groups:
        raise ValueError("No samples")
    return groups


def paired_ratio(samples, rng):
    repeats = {rep for rep, _ in samples}
    ratios = [samples[rep, "linear"] / samples[rep, "radix"] for rep in sorted(repeats)]
    medians = sorted(
        statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(10_000)
    )
    return statistics.median(ratios), medians[250], medians[9749]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    args = parser.parse_args()
    rng = random.Random(61796)
    print("layout,routes,linear_median_ns,radix_median_ns,ratio,paired_ci95_low,paired_ci95_high")
    for (layout, size), samples in sorted(read_samples(args.csv).items()):
        linear, radix = [
            statistics.median(v for (_, name), v in samples.items() if name == matcher)
            for matcher in ("linear", "radix")
        ]
        ratio, low, high = paired_ratio(samples, rng)
        print(f"{layout},{size},{linear:.2f},{radix:.2f},{ratio:.3f},{low:.3f},{high:.3f}")


if __name__ == "__main__":
    main()
