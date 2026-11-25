"""Adapt native HTTP/1.1 offer rows to the historical increment 7 auditor."""

import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "program"))
from capacity import audit, digest


def prepare(rate, seconds, warmup, raw, summary, warm, resume, done, release):
    spec = {
        "url": "http://proxy:8080/echo", "protocol": "http1", "rate": int(rate),
        "seconds": int(seconds), "warmup_seconds": int(warmup), "concurrency": 32, "queue": 64,
        "request_bytes": 0, "response_bytes": 1024, "response_byte": ord("x"),
        "warmup_done_file": f"/results/{warm.name}", "resume_file": f"/results/{resume.name}",
        "measurement_done_file": f"/results/{done.name}", "measurement_release_file": f"/results/{release.name}",
    }
    spec_path = raw.with_suffix(".native-spec.json")
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    return ["/results/cauce-load", f"/results/raw/{spec_path.name}", f"/results/raw/{raw.name}",
            f"/results/raw/{summary.with_suffix('.native.json').name}"]


def control(dc_run, project, env, outdir, rate, seconds, warmup, label):
    spec = {
        "url": "http://backend:8000/echo", "protocol": "http1", "rate": int(rate) * 2,
        "seconds": int(seconds), "warmup_seconds": int(warmup), "concurrency": 32, "queue": 64,
        "request_bytes": 0, "response_bytes": 1024, "response_byte": ord("x"),
    }
    specfile, raw, summary = [outdir / f"{label}.{suffix}"
                              for suffix in ("spec.json", "offers.jsonl", "summary.json")]
    specfile.write_text(json.dumps(spec, indent=2) + "\n")
    dc_run(["-p", project, "--profile", "client", "run", "--no-deps", "-T", "--rm",
            "--entrypoint", "/results/cauce-load", "client",
            *(f"/results/{path.name}" for path in (specfile, raw, summary))],
           env=env, timeout=seconds + warmup + 30)
    result = audit(raw, json.loads(summary.read_text()), spec)
    (outdir / f"{label}.audit.json").write_text(json.dumps(result, indent=2) + "\n")
    if (result["invalid_reasons"] or result["successful"] != result["offered"]
            or result["warmup_successes"] != result["warmup_offers"]):
        raise RuntimeError(f"generator/backend control failed: {label}")
    return result


def project_rows(raw):
    labels = {
        "scheduler_drop": "dropped", "queue_rejection": "rejected",
        "queue_timeout": "timeout", "request_timeout": "timeout",
        "body_error": "validation_error", "protocol_error": "validation_error",
        "dispatcher_closed": "error", "transport_error": "error",
    }
    rows = [json.loads(line) for line in raw.read_text().splitlines()]
    for row in rows:
        row["native_outcome"] = row["outcome"]
        row["outcome"] = labels.get(row["outcome"], row["outcome"])
        row["scheduled_latency_ms"] = (row["end_ns"] - row["scheduled_ns"]) / 1e6
        row["scheduler_lag_ms"] = (row["arrival_ns"] - row["scheduled_ns"]) / 1e6
    return rows


def normalize(raw, summary):
    native = json.loads(summary.with_suffix(".native.json").read_text())
    spec = json.loads(raw.with_suffix(".native-spec.json").read_text())
    checked = audit(raw, native, spec)
    summary.with_suffix(".native-audit.json").write_text(json.dumps(checked, indent=2) + "\n")
    rows = project_rows(raw)

    def quantiles(values):
        values = sorted(values)
        return {name: values[math.ceil(p * len(values)) - 1] if values else None
                for name, p in (("p50", .50), ("p95", .95), ("p99", .99))}

    success = [r["scheduled_latency_ms"] for r in rows if r["outcome"] == "success"]
    outcomes = checked["outcomes"]
    duration = max(spec["seconds"], max(r["end_ns"] for r in rows) / 1e9)
    projected = {
        "schema": "cauce.increment7.native-adapter.v1",
        "offered": checked["offered"], "offered_rate_per_second": spec["rate"],
        "offered_seconds": spec["seconds"], "successful_responses": len(success),
        "offer_window_goodput_per_second": checked["goodput_per_s"],
        "offered_goodput_per_second": len(success) / spec["seconds"],
        "total_goodput_per_second": len(success) / duration,
        "errors": sum(outcomes.get(k, 0) for k in
                      ("dispatcher_closed", "transport_error", "body_error", "protocol_error")),
        "http_errors": outcomes.get("http_error", 0),
        "rejections": outcomes.get("queue_rejection", 0), "drops": outcomes.get("scheduler_drop", 0),
        "timeouts": outcomes.get("queue_timeout", 0) + outcomes.get("request_timeout", 0),
        "warmup_requests_offered": checked["warmup_offers"],
        "warmup_requests_excluded": checked["warmup_successes"],
        "warmup_errors": checked["warmup_offers"] - checked["warmup_successes"],
        "warmup_rejections": 0,
        "connection_new": None, "connection_reused": None,
        "percentiles_ms": {
            "scheduled_latency": {"success": quantiles(success)},
            "scheduler_lag": {"all": quantiles(r["scheduler_lag_ms"] for r in rows)},
        },
    }
    summary.write_text(json.dumps(projected, indent=2) + "\n")
