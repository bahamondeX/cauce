"""Regression checks for exact-equivalence evidence admission."""
import json
from pathlib import Path
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = runpy.run_path(str(ROOT / "scripts/nginx-gap-audit.py"))
REPORT = "bench/program/evidence/milestone-10/differential-final.json"
MODULE = "ngx_http_core_module"


class EquivalenceEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report = json.loads((ROOT / REPORT).read_text())
        cls.results = next(item for item in report["modules"] if item["module"] == MODULE)

    def review(self, names):
        proof = {
            "criteria": list(AUDIT["VERIFICATION_CRITERIA"]),
            "differential_report": REPORT,
            "cases": names,
        }
        return AUDIT["verify_claim"](MODULE, "location", {"verification": proof})

    def test_accepts_named_two_engine_case(self):
        case = self.results["cases"][0]["case"]
        self.assertEqual(self.review([case])["cases"], [case])

    def test_rejects_empty_unknown_and_one_engine_claims(self):
        one_engine = next(item["case"] for item in self.results["configuration_rejections"]
                          if set(item["observations"]) == {"cauce"})
        for names in ([], ["unknown-case"], [one_engine]):
            with self.subTest(names=names), self.assertRaisesRegex(AssertionError, "missing named cases"):
                self.review(names)
