import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capacity import audit, campaign, passes


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


class BracketTests(unittest.TestCase):
    def run_campaign(self, limit=350, control_limit=2_000, confirmation=True, postflight=True):
        attempted = []
        report = {}

        def run(spec, label):
            attempted.append((label, spec["rate"]))
            passes_at_rate = (
                spec["rate"] <= control_limit if label.startswith("control") else spec["rate"] <= limit
            )
            if label.startswith("confirm"):
                passes_at_rate &= confirmation
            if label == "control-after":
                passes_at_rate &= postflight
            return {
                "invalid_reasons": [], "scheduled_latency_ns": {"p99": 1, "p99.9": 1},
                "warmup_offers": 1, "warmup_successes": 1, "rate": spec["rate"],
                "offered": 1, "successful": int(passes_at_rate),
                "goodput_per_s": spec["rate"] if passes_at_rate else 0,
            }

        status = campaign(run, report, {}, {}, [100, 250, 500, 1000], (20, 100, .99, 0), 60, 2, 180)
        return status, report, attempted

    def test_refinement_searches_passing_to_failing_bracket(self):
        status, report, attempts = self.run_campaign()
        self.assertEqual(status, "CONFIRMED")
        self.assertEqual(report["highest_tested_confirmed_rate"], 312)
        self.assertEqual(report["first_failing_rate"], 375)
        self.assertEqual(report["interpretation"], "confirmed_bracket")
        self.assertEqual(attempts[-3:], [("confirm-312", 312), ("stability-312", 312), ("control-after", 1000)])

    def test_control_failure_preserves_smaller_confirmed_lower_bound(self):
        status, report, attempts = self.run_campaign(control_limit=600)
        self.assertEqual(status, "CONFIRMED")
        self.assertEqual(report["control_limited_rate"], 500)
        self.assertIsNone(report["first_failing_rate"])
        self.assertEqual(report["interpretation"], "confirmed_lower_bound")
        self.assertNotIn(("ramp-500", 500), attempts)
        self.assertEqual(attempts[-1], ("control-after", 500))

    def test_control_and_confirmation_cannot_promote_failed_run(self):
        status, report, _ = self.run_campaign(confirmation=False)
        self.assertEqual(status, "CONFIRMATION_FAILED")
        self.assertNotIn("confirmed_goodput_per_s", report)
        status, report, _ = self.run_campaign(postflight=False)
        self.assertEqual(status, "REJECTED_POSTFLIGHT")
        self.assertNotIn("confirmed_goodput_per_s", report)


if __name__ == "__main__":
    unittest.main()
