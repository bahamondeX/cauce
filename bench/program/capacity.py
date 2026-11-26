#!/usr/bin/env python3
"""Qualify independent generator/backend headroom, then ramp and confirm goodput."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

JITTER_P99_NS = 10_000_000
LOAD_SCHEMA = "cauce.open-loop.v1"


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2) + "\n")


def digest(path):
    with path.open("rb") as stream:
        hash_value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hash_value.update(block)
        return hash_value.hexdigest()


def quantile(values, fraction):
    return sorted(values)[math.ceil(fraction * len(values)) - 1] if values else None


def validated_offers(path, spec):
    with path.open() as stream:
        for n, line in enumerate(stream):
            row = json.loads(line)
            scheduled = n * 1_000_000_000 // spec["rate"]
            arrival, start, end = (row[key] for key in ("arrival_ns", "start_ns", "end_ns"))
            if (row["n"] != n or row["scheduled_ns"] != scheduled
                    or not scheduled <= arrival <= end
                    or (start is not None and not arrival <= start <= end)):
                raise ValueError(f"{path.name} offer {n} has invalid identity or timestamp ordering")
            if row["outcome"] == "success" and (
                start is None or not 200 <= row["status"] < 300 or row["bytes"] != spec["response_bytes"]
            ):
                raise ValueError(f"{path.name} offer {n} has an inconsistent success record")
            yield row


def audit(raw, summary, spec):
    expected = spec["rate"] * spec["seconds"]
    if summary["schema"] != LOAD_SCHEMA or summary["offered"] != expected:
        raise ValueError("client summary count or schema differs")
    outcomes = {}
    jitter, latency, queue, service = [], [], [], []
    within = 0
    for row in validated_offers(raw, spec):
        scheduled, arrival, start, end = (row[key] for key in ("scheduled_ns", "arrival_ns", "start_ns", "end_ns"))
        jitter.append(arrival - scheduled)
        outcome = row["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if start is not None:
            queue.append(start - arrival)
        if outcome == "success":
            latency.append(end - scheduled)
            service.append(end - start)
            within += end <= spec["seconds"] * 1_000_000_000
    if expected != len(jitter) or outcomes != summary["outcomes"] or within != summary["offer_window_successes"]:
        raise ValueError("raw offer counts differ from summary")
    if summary["warmup_offers"] != spec["rate"] * spec["warmup_seconds"]:
        raise ValueError("warm-up offer count differs from spec")
    warmup_outcomes = {}
    if summary["warmup_offers"]:
        for row in validated_offers(raw.with_suffix(".warmup.jsonl"), spec):
            outcome = row["outcome"]
            warmup_outcomes[outcome] = warmup_outcomes.get(outcome, 0) + 1
        if (sum(warmup_outcomes.values()), warmup_outcomes.get("success", 0)) != (
            summary["warmup_offers"], summary["warmup_successes"]
        ):
            raise ValueError("warm-up raw counts differ from summary")
    contaminated = []
    if quantile(jitter, .99) > JITTER_P99_NS:
        contaminated.append("scheduler p99 exceeds 10 ms")
    if outcomes.get("scheduler_drop", 0) or outcomes.get("dispatcher_closed", 0):
        contaminated.append("scheduler or dispatcher lost offers")
    return {
        "invalid_reasons": contaminated, "outcomes": outcomes, "offered": expected, "rate": spec["rate"],
        "successful": len(latency), "offer_window_successes": within,
        "goodput_per_s": within / spec["seconds"],
        "scheduled_latency_ns": {name: quantile(latency, p) for name, p in
                                 (("p50", .50), ("p95", .95), ("p99", .99), ("p99.9", .999))},
        "queue_p99_ns": quantile(queue, .99), "service_p99_ns": quantile(service, .99),
        "scheduler_p99_ns": quantile(jitter, .99), "warmup_successes": summary["warmup_successes"],
        "warmup_offers": summary["warmup_offers"], "warmup_outcomes": warmup_outcomes,
    }


def state():
    result = {}
    for name in ("cpu", "memory", "io"):
        path = Path("/proc/pressure") / name
        if path.is_file():
            result[name] = path.read_text()
    return result


def proc_sample(pid, details=False):
    proc = Path("/proc") / str(pid)
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    sample = {
        "cpu_ticks": int(fields[11]) + int(fields[12]),
        "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
        "time_ns": time.monotonic_ns(),
    }
    if details:
        sample["pss_bytes"] = None
        try:
            for line in (proc / "smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    sample["pss_bytes"] = int(line.split()[1]) * 1024
                    break
        except OSError:
            pass
    return sample


def measure_process(command, log, path, spec, pid):
    before, after, peak = None, None, 0
    timeout = spec["seconds"] + spec["warmup_seconds"] + 30
    limit = time.monotonic() + timeout
    with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as child:
        try:
            next_sample = 0
            while child.poll() is None:
                now = time.monotonic()
                if now >= limit:
                    raise subprocess.TimeoutExpired(command, timeout)
                if before is None and (path / "warmup.done").exists():
                    before = proc_sample(pid, details=True)
                    peak = before["rss_bytes"]
                    (path / "measure.start").touch()
                if after is None and before is not None:
                    if (path / "measurement.done").exists():
                        after = proc_sample(pid, details=True)
                        peak = max(peak, after["rss_bytes"])
                        (path / "measure.release").touch()
                    elif now >= next_sample:
                        peak = max(peak, proc_sample(pid)["rss_bytes"])
                        next_sample = now + 0.25
                time.sleep(0.002)
        finally:
            if child.poll() is None:
                child.kill()
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, command)
    if before is None or after is None:
        raise ValueError("generator phase markers missing")
    return {"before": before, "after": after, "rss_peak_sampled_bytes": peak,
            "cpu_ticks_per_second": os.sysconf("SC_CLK_TCK")}


def run_cell(binary, root, spec, label, prefix=(), sample_pid=None):
    path = root / label
    path.mkdir()
    command = [*prefix, str(binary), str(path / "spec.json"), str(path / "offers.jsonl"), str(path / "summary.json")]
    if sample_pid is not None:
        spec = {**spec, **{
            key: str((path / name).resolve()) for key, name in (
                ("warmup_done_file", "warmup.done"), ("resume_file", "measure.start"),
                ("measurement_done_file", "measurement.done"), ("measurement_release_file", "measure.release"),
            )
        }}
    write_json(path / "spec.json", spec)
    started = time.monotonic_ns()
    before = state()
    try:
        with (path / "client.log").open("w") as log:
            if sample_pid is None:
                subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True,
                               timeout=spec["seconds"] + spec["warmup_seconds"] + 30)
            else:
                resources = measure_process(command, log, path, spec, sample_pid)
        result = audit(path / "offers.jsonl", json.loads((path / "summary.json").read_text()), spec)
        if sample_pid is not None:
            ticks = resources["after"]["cpu_ticks"] - resources["before"]["cpu_ticks"]
            result["process"] = resources
            result["cpu_us_per_success"] = (1e6 * ticks / resources["cpu_ticks_per_second"]
                                            / result["successful"] if result["successful"] else None)
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as error:
        result = {"invalid_reasons": [str(error)]}
    result.update(label=label, rate=spec["rate"], seconds=spec["seconds"],
                  elapsed_ns=time.monotonic_ns() - started, pressure_before=before, pressure_after=state())
    write_json(path / "audit.json", result)
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("pressure")}), flush=True)
    return result


def passes(result, p99_ns, p999_ns, min_goodput, max_error):
    if result["invalid_reasons"]:
        return False
    quantiles = result["scheduled_latency_ns"]
    return (
        quantiles["p99"] is not None and quantiles["p99"] <= p99_ns and quantiles["p99.9"] <= p999_ns
        and result["warmup_offers"] == result["warmup_successes"]
        and (result["offered"] - result["successful"]) / result["offered"] <= max_error
        and result["goodput_per_s"] >= min_goodput * result["rate"]
    )


def ramp_failure_reason(result):
    if result["warmup_successes"] != result["warmup_offers"]:
        return "warmup_loss"
    if any(result.get("outcomes", {}).get(outcome, 0)
           for outcome in ("queue_rejection", "queue_timeout")):
        return "generator_queue_loss"
    return "measured_slo"


def campaign(run, report, target, control, rates, limits, confirm_seconds,
             refine_steps=0, stability_seconds=0):
    """Search an audited bracket and verify the candidate on a longer hold."""
    best = None
    upper = None
    upper_evidence = None
    control_ceiling = None
    contamination = None
    qualified_control = 0
    control_limits = (*limits[:3], 0)

    def ramp(rate, label):
        nonlocal qualified_control, control_ceiling, contamination, best, upper, upper_evidence
        if 2 * rate > qualified_control:
            before = run({**control, "rate": 2 * rate}, f"control-before-{rate}")
            if not passes(before, *control_limits):
                control_ceiling = rate
                return False
            qualified_control = 2 * rate
        result = run({**target, "rate": rate}, label)
        if result["invalid_reasons"]:
            contamination = rate
            return False
        if passes(result, *limits):
            best = rate
            return True
        upper = rate
        upper_evidence = ramp_failure_reason(result)
        return False

    for rate in rates:
        if not ramp(rate, f"ramp-{rate}"):
            break

    for _ in range(refine_steps):
        if best is None or upper is None or upper - best <= 1 or contamination is not None:
            break
        rate = (best + upper) // 2
        ramp(rate, f"refine-{rate}")

    tested_boundary = upper if upper_evidence == "measured_slo" else None
    report.update(highest_passing_ramp=best, first_failing_rate=tested_boundary,
                  unattributed_rate=upper if tested_boundary is None else None,
                  upper_rate_evidence=upper_evidence,
                  control_limited_rate=control_ceiling, contaminated_rate=contamination)
    if best is None:
        return ("REJECTED_PREFLIGHT" if control_ceiling else
                "CONTAMINATED" if contamination else "NO_QUALIFYING_RAMP")

    # Recheck the highest audited bracket rate even if a lower candidate is
    # confirmed. A failed postflight invalidates attribution for the bracket.
    confirm = run({**target, "rate": best, "seconds": confirm_seconds}, f"confirm-{best}")
    stable = passes(confirm, *limits)
    if stable and stability_seconds:
        stability = run({**target, "rate": best, "seconds": stability_seconds}, f"stability-{best}")
        stable = passes(stability, *limits)
        report["stability_passed"] = stable
    after = run({**control, "rate": qualified_control}, "control-after")
    if not passes(after, *control_limits):
        return "REJECTED_POSTFLIGHT"
    if not passes(confirm, *limits):
        return "CONFIRMATION_FAILED"
    report["highest_tested_confirmed_rate"] = best
    report["confirmed_goodput_per_s"] = confirm["goodput_per_s"]
    if not stable:
        return "STABILITY_FAILED"
    report["interpretation"] = ("confirmed_bracket" if tested_boundary else "confirmed_lower_bound")
    return "CONFIRMED"


def main():

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("target", type=Path, help="base measurement spec JSON")
    parser.add_argument("control", type=Path, help="direct fixture spec with its own authority and CA")
    parser.add_argument("output", type=Path)
    parser.add_argument("--rates", type=int, nargs="+", required=True)
    parser.add_argument("--confirm-seconds", type=int, required=True)
    parser.add_argument("--refine-steps", type=int, default=0)
    parser.add_argument("--stability-seconds", type=int, default=0)
    parser.add_argument("--client-cpus", help="Linux taskset CPU list for the generator")
    parser.add_argument("--sample-pid", type=int, help="sample this Linux proxy during target cells")
    parser.add_argument("--p99-ms", type=float, required=True)
    parser.add_argument("--p999-ms", type=float, required=True)
    parser.add_argument("--min-goodput", type=float, default=.99)
    parser.add_argument("--max-error", type=float, default=0)
    args = parser.parse_args()
    if (args.rates != sorted(set(args.rates)) or args.rates[0] <= 0
            or not 0 <= args.refine_steps <= 20 or args.stability_seconds < 0
            or not 0 < args.min_goodput <= 1 or not 0 <= args.max_error < 1
            or not 0 < args.p99_ms <= args.p999_ms < float("inf")):
        parser.error("rates must increase and SLO values must be valid")
    target, control = [json.loads(path.read_text()) for path in (args.target, args.control)]
    if set(target) & {"warmup_done_file", "resume_file", "measurement_done_file", "measurement_release_file"}:
        parser.error("capacity runner owns phase timing")
    if set(control) & {"warmup_done_file", "resume_file", "measurement_done_file", "measurement_release_file"}:
        parser.error("control spec cannot configure phase barriers")
    if any(target.get(key) != control.get(key) for key in
           ("protocol", "request_bytes", "response_bytes", "response_byte")):
        parser.error("the control must preserve protocol and payload")
    if args.stability_seconds and args.stability_seconds < args.confirm_seconds:
        parser.error("stability hold must be at least as long as confirmation")
    if not 0 < target["seconds"] < args.confirm_seconds:
        parser.error("confirmation must be longer than each ramp")
    target, control = dict(target), dict(control)
    args.output.mkdir(parents=True)
    binary = args.binary.resolve()
    write_json(args.output / "environment.json", {
        "platform": platform.platform(), "clock": vars(time.get_clock_info("monotonic")),
        "binary_sha256": digest(binary),
        "affinity": sorted(os.sched_getaffinity(0)) if sys.platform == "linux" else None,
    })
    report = {"status": "PREFLIGHT", "caps": {
        "p99_ms": args.p99_ms, "p999_ms": args.p999_ms, "max_error_fraction": args.max_error,
        "min_goodput_fraction": args.min_goodput, "jitter_p99_ns": JITTER_P99_NS,
    }, "cells": []}
    manifest = args.output / "campaign.json"

    prefix = ["taskset", "-c", args.client_cpus] if args.client_cpus else []

    def run(spec, label):
        sample_pid = args.sample_pid if not label.startswith("control-") else None
        result = run_cell(binary, args.output, spec, label, prefix, sample_pid)
        report["cells"].append(result)
        write_json(manifest, report)
        return result

    limits = (args.p99_ms * 1e6, args.p999_ms * 1e6, args.min_goodput, args.max_error)
    report["status"] = campaign(run, report, target, control, args.rates, limits,
                                args.confirm_seconds, args.refine_steps, args.stability_seconds)
    write_json(manifest, report)
    return 0 if report["status"] == "CONFIRMED" else 2


if __name__ == "__main__":
    sys.exit(main())
