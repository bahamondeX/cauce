import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/nginx-migration-report.py"


class MigrationTests(unittest.TestCase):
    def run_preview(self, names, locations):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = (Path(tmp) / f for f in ("nginx.conf", "preview.yaml"))
            source.write_text(f"""http {{
              server {{
                listen 127.0.0.1:8080;
                server_name {names};
                {locations}
              }}
            }}""")
            result = subprocess.run([sys.executable, str(TOOL), str(source), "--preview",
                                     str(destination)], capture_output=True, check=False, text=True)
            report = json.loads(result.stdout)
            config = yaml.safe_load(destination.read_text()) if destination.is_file() else None
            if config and (ROOT / "target/debug/cauce").is_file():
                validation = subprocess.run([str(ROOT / "target/debug/cauce"), "check", str(destination)],
                                            capture_output=True, check=False)
                self.assertEqual(validation.returncode, 0, validation.stderr)
            return result.returncode, report, config

    def test_literal_locations_and_aliases_have_review_and_validation_metadata(self):
        code, report, config = self.run_preview("API.example.org *.example.org mail.* API.example.org",
            "location /health { proxy_pass http://127.0.0.1:2010; } "
            "location = /health { proxy_pass http://127.0.0.1:2011; }")
        self.assertEqual(code, 0)
        self.assertEqual(report["preview"]["confidence"], "manual_review")
        self.assertEqual(report["preview"]["validation"][:2], ["cauce", "check"])
        rows = config["proxy"]["routes"]
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(r.get("path_match") == "exact" for r in rows), 6)
        self.assertIn("*.example.org:8080", {r["host"] for r in rows})

    def test_unsupported_host_or_location_refuses_partial_preview(self):
        for names, locations in [
            ("foo.*.org", "location / { proxy_pass http://127.0.0.1:2010; }"),
            ("foo.org", "location ~ /regex { proxy_pass http://127.0.0.1:2010; }"),
            ("foo.org", "location = / { proxy_pass http://127.0.0.1:2010; } include other.conf;"),
        ]:
            with self.subTest(names=names, locations=locations):
                code, report, config = self.run_preview(names, locations)
                self.assertEqual(code, 1)
                self.assertEqual(report["preview"]["confidence"], "unsupported_subset")
                self.assertIsNone(config)
