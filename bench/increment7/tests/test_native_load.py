import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_load import normalize


class NativeLoadProjectionTests(unittest.TestCase):
    def test_projection_preserves_raw_native_offers(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "cell.jsonl"
            summary = Path(directory) / "cell.summary.json"
            original = json.dumps({
                "n": 0, "scheduled_ns": 0, "arrival_ns": 100, "start_ns": 200, "end_ns": 1000,
                "status": 200, "bytes": 1024, "outcome": "success",
            }) + "\n"
            raw.write_text(original)
            raw.chmod(0o444)
            raw.with_suffix(".native-spec.json").write_text(json.dumps({
                "rate": 1, "seconds": 1, "warmup_seconds": 0, "response_bytes": 1024,
            }))
            summary.with_suffix(".native.json").write_text(json.dumps({
                "schema": "cauce.open-loop.v1", "offered": 1, "outcomes": {"success": 1},
                "offer_window_successes": 1, "warmup_offers": 0, "warmup_successes": 0,
            }))
            normalize(raw, summary)
            report = json.loads(summary.read_text())
            self.assertEqual(report["offer_window_goodput_per_second"], 1)
            self.assertEqual(report["percentiles_ms"]["scheduled_latency"]["success"]["p99"], .001)
            self.assertEqual(raw.read_text(), original)
