#!/usr/bin/env python3
"""Audit staged campaign offers and export qualified capacity and scaling rows."""

import argparse
import csv
import json
from pathlib import Path

from capacity import audit, passes, write_json


LIMITS = (20_000_000, 100_000_000, .99, 0)
AUDITED = ("offered", "successful", "outcomes", "offer_window_successes",
           "goodput_per_s", "scheduled_latency_ns", "scheduler_p99_ns",
           "warmup_offers", "warmup_successes")


def read(path):
    return json.loads(path.read_text())


def check_cell(path):
    archived = read(path / "audit.json")
    if "offered" not in archived:
        return {"offers": 0, "qualified": False, "invalid": archived["invalid_reasons"]}
    raw = audit(path / "offers.jsonl", read(path / "summary.json"), read(path / "spec.json"))
    for field in AUDITED:
        if raw[field] != archived[field]:
            raise ValueError(f"{path} disagrees with raw offers on {field}")
    return {"offers": raw["offered"], "qualified": passes(raw, *LIMITS),
            "invalid": raw["invalid_reasons"], "successful": raw["successful"]}


def parse_campaign(folder, run):
    report = read(folder / "campaign.json")
    cells = {path.parent.name: check_cell(path.parent) for path in sorted(folder.glob("*/audit.json"))}
    confirmed = report.get("confirmed_goodput_per_s") if report["status"] == "CONFIRMED" else None
    if confirmed is not None:
        rate = report["highest_tested_confirmed_rate"]
        control = cells["control-after"]
        confirm_path = folder / f"confirm-{rate}"
        if not control["qualified"] or not cells[f"confirm-{rate}"]["qualified"]:
            raise ValueError(f"{run.name} requires a qualifying postflight and confirmation")
        if read(confirm_path / "spec.json")["seconds"] < 60:
            raise ValueError(f"{run.name} confirmation shorter than 60 seconds")
        if report.get("stability_passed") and not cells[f"stability-{rate}"]["qualified"]:
            raise ValueError(f"{run.name} stability flag disagrees with offers")
    row = {"run": run.name, "status": report["status"],
           "confirmed_goodput_per_s": confirmed,
           "interpretation": report.get("interpretation"),
           "first_failing_rate": report.get("first_failing_rate"),
           "control_limited_rate": report.get("control_limited_rate")}
    if confirmed is not None:
        summary = read(confirm_path / "audit.json")
        row.update(cpu_us_per_success=summary["cpu_us_per_success"],
                   rss_peak_bytes=summary["process"]["rss_peak_sampled_bytes"],
                   pss_end_bytes=summary["process"]["after"]["pss_bytes"],
                   **summary["scheduled_latency_ns"])
    return row, cells


def fit_scaling(rows):
    by_label = {row["run"]: row for row in rows}
    candidates = {}
    for workers in (1, 2, 4, 8):
        label = f"cauce-{workers}w"
        primary = by_label.get(label)
        fallback = by_label.get(label + "-conservative")
        selected = fallback if fallback and fallback["status"] == "CONFIRMED" else primary
        if selected:
            candidates[workers] = selected
    baseline = candidates.get(1, {})
    for workers, row in sorted(candidates.items()):
        lower = row["confirmed_goodput_per_s"]
        both_bracketed = row["interpretation"] == baseline.get("interpretation") == "confirmed_bracket"
        yield {"workers": workers, "run": row["run"], "confirmed_goodput_per_s": lower,
               "interpretation": row["interpretation"], "status": row["status"],
               "scaling_efficiency": lower / (workers * baseline["confirmed_goodput_per_s"])
               if both_bracketed and lower else None}


def write_csv(path, rows, columns):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    evidence = {"runs": {}, "measured_offers": 0}
    rows = []
    for run in sorted((args.results / "first-pass").iterdir()):
        if (run / "measurements/campaign.json").is_file():
            row, cells = parse_campaign(run / "measurements", run)
            rows.append(row)
        else:
            cells = {path.parent.name: check_cell(path.parent) for path in sorted(run.glob("*/audit.json"))}
            if not cells:
                continue
            matched = run / "matched.json"
            if matched.is_file() and read(matched)["qualified"] and not all(c["qualified"] for c in cells.values()):
                raise ValueError(f"{run.name} matched flag disagrees with raw offers")
        evidence["runs"][run.name] = cells
        evidence["measured_offers"] += sum(c["offers"] for c in cells.values())
    write_json(args.output / "raw-audit.json", evidence)
    write_csv(args.output / "capacity.csv", rows, (
        "run", "status", "confirmed_goodput_per_s", "interpretation", "first_failing_rate",
        "control_limited_rate", "cpu_us_per_success", "p50", "p95", "p99", "p99.9",
        "rss_peak_bytes", "pss_end_bytes",
    ))
    write_csv(args.output / "scaling.csv", fit_scaling(rows),
              ("workers", "run", "confirmed_goodput_per_s", "interpretation", "status", "scaling_efficiency"))


if __name__ == "__main__":
    main()
