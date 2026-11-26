"""Reconstruct observable offer phases without assigning unseen queue owners."""
from bisect import bisect_left, bisect_right
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]) / "first-pass"
result = {}
for label, cell in (
    ("cauce-1w", "ramp-8000"), ("cauce-1w", "control-before-8000"),
    ("cauce-2w", "control-after"), ("cauce-8w", "control-before-16000"),
):
    fixture = root / label / "measurements" / cell
    rows = [json.loads(line) for line in (fixture/"offers.warmup.jsonl").open()]
    admitted = [row for row in rows if row["start_ns"] is not None]
    arrived = sorted(row["arrival_ns"] for row in admitted)
    started = sorted(row["start_ns"] for row in admitted)
    finished = sorted(row["end_ns"] for row in admitted)
    errors = [row for row in rows if row["outcome"] == "queue_rejection"]
    summary = dict(offers=len(rows), observed_starts=len(started), queue_rejections=len(errors),
                   rate=json.loads((fixture/"spec.json").read_text())["rate"])
    if errors:
        first, last = errors[0]["arrival_ns"], errors[-1]["arrival_ns"]
        summary.update(first_rejection_ns=first, last_rejection_ns=last,
                       arrivals_before_first=bisect_right(arrived,first),
                       started_not_finished_at_first=bisect_right(started,first)-bisect_right(finished,first),
                       arrived_without_start_at_first=bisect_right(arrived,first)-bisect_right(started,first),
                       completions_during_rejections=bisect_right(finished,last)-bisect_left(finished,first))
        ended_before = bisect_left(finished,first)
        ended_after = bisect_right(finished,last)
        if ended_before and ended_after < len(finished):
            summary.update(last_completion_before_loss_ns=finished[ended_before-1],
                           first_completion_after_loss_ns=finished[ended_after])
    result[f"{label}/{cell}"] = summary
print(json.dumps(result, indent=2))
