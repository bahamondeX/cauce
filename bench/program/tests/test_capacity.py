import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capacity import audit, passes


class CapacityAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "offers.jsonl"
        self.spec = {"rate": 2, "seconds": 1, "response_bytes": 4, "warmup_seconds": 0}
        self.rows = [
            {"n": n, "scheduled_ns": n * 500_000_000, "arrival_ns": n * 500_000_000 + 10,
             "start_ns": n * 500_000_000 + 20, "end_ns": n * 500_000_000 + 30,
             "status": 200, "bytes": 4, "outcome": "success"}
            for n in range(2)
        ]
        self.summary = {
            "schema": "cauce.open-loop.v1", "offered": 2, "outcomes": {"success": 2},
            "offer_window_successes": 2, "warmup_offers": 0, "warmup_successes": 0,
        }

    def inspect(self):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        return audit(self.path, self.summary, self.spec)

    def test_scheduled_arrival_is_origin_for_slo(self):
        measurement = self.inspect()
        self.assertEqual(measurement["scheduled_latency_ns"]["p99"], 30)
        self.assertEqual(measurement["service_p99_ns"], 10)
        self.assertTrue(passes(measurement, 30, 30, 1, 0))
        self.assertFalse(passes(measurement, 29, 29, 1, 0))

    def test_duplicate_offer_is_rejected(self):
        self.rows[1]["n"] = 0
        with self.assertRaisesRegex(ValueError, "invalid identity"):
            self.inspect()

    def test_infrastructure_jitter_rejects_intact_response(self):
        row = self.rows[1]
        row.update(arrival_ns=520_000_000, start_ns=520_000_001, end_ns=520_000_002)
        measurement = self.inspect()
        self.assertEqual(measurement["successful"], 2)
        self.assertEqual(measurement["invalid_reasons"], ["scheduler p99 exceeds 10 ms"])
        self.assertFalse(passes(measurement, 100_000_000, 100_000_000, 1, 0))


if __name__ == "__main__":
    unittest.main()
