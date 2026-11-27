"""Exercise both promotion and rejection of registration-level evidence."""
import json
from pathlib import Path
import runpy
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDIT = runpy.run_path(str(ROOT / "scripts/nginx-gap-audit.py"))
REPORT = "bench/program/evidence/milestone-11/internal-proof.json"
MODULE = "ngx_http_core_module"
REGISTRATION = "oss/src/http/ngx_http_core_module.c/ngx_http_core_module/internal/29"
CONTEXTS = ["NGX_HTTP_LOC_CONF"]


class EquivalenceEvidence(unittest.TestCase):
    def review(self, report):
        mapping = {"verification": {"differential_report": str(report)}}
        return AUDIT["verify_claim"](MODULE, "internal", mapping, REGISTRATION, CONTEXTS)

    def test_accepts_a_complete_registration_proof(self):
        evidence = self.review(REPORT)
        self.assertIn("generation-after", evidence["cases"])
        self.assertIn("reject-argument", evidence["cases"])

    def test_rejects_module_only_evidence(self):
        with self.assertRaisesRegex(ValueError, "registration and fixture mismatch"):
            self.review("bench/program/evidence/milestone-10/differential-final.json")

    def test_rejects_a_missing_or_one_engine_case(self):
        work = ROOT / "work"
        work.mkdir(exist_ok=True)
        for mutation in ("remove", "one-engine"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=work) as folder:
                report = json.loads((ROOT / REPORT).read_text())
                rows = report["modules"][0]["configuration_rejections"]
                case = next(row for row in rows if row["case"] == "reject-argument")
                if mutation == "remove":
                    rows.remove(case)
                else:
                    del case["observations"]["cauce"]
                target = Path(folder) / "report.json"
                target.write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, "missing passing cases for invalid_arguments"):
                    self.review(target)
