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
    def run_preview(self, names, locations, server_extra='', listener='127.0.0.1:8080'):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = (Path(tmp) / f for f in ("nginx.conf", "preview.yaml"))
            source.write_text(f"""http {{
              server {{
                listen {listener};
                server_name {names};
                {server_extra}
                {locations}
              }}
            }}""")
            result = subprocess.run([sys.executable, str(TOOL), str(source), "--preview",
                                     str(destination)], capture_output=True, check=False, text=True)
            report = json.loads(result.stdout)
            config = yaml.safe_load(destination.read_text()) if destination.is_file() else None
            if config:
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            if (config and not config["proxy"].get("tls")
                    and not any(route.get("content",{}).get("type")=="static" for route in config["proxy"]["routes"])
                    and (ROOT / "target/debug/cauce").is_file()):
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
        self.assertEqual(sum(r.get("path_match") == "literal_prefix" for r in rows), 6)
        self.assertIn("*.example.org:8080", {r["host"] for r in rows})

    def test_no_regex_prefix_is_preserved(self):
        code, _, config = self.run_preview("example.org",
            "location ^~ /images { proxy_pass http://127.0.0.1:2010; }")
        self.assertEqual(code, 0)
        self.assertTrue(all(route["path_match"] == "prefix_no_regex"
                            for route in config["proxy"]["routes"]))

    def test_literal_proxy_headers_are_previewed_without_reporting_values(self):
        code, report, config = self.run_preview("example.org",
            'location / { proxy_pass http://127.0.0.1:2010; '
            'proxy_set_header x-tenant local-marker; proxy_set_header x-old ""; '
            'proxy_hide_header X-Internal; }')
        self.assertEqual(code, 0)
        self.assertNotIn("local-marker", json.dumps(report))
        self.assertEqual(config["proxy"]["routes"][0]["headers"], {
            "request": {"set": {"x-tenant": "local-marker"}, "remove": ["x-old"]},
            "response": {"remove": ["x-internal"]},
        })
        for directive in ["proxy_set_header X-Forwarded-For 192.0.2.1;", "proxy_set_header X-Tenant $host;"]:
            with self.subTest(directive=directive):
                code, _, config = self.run_preview("example.org",
                    "location / { proxy_pass http://127.0.0.1:2010; " + directive + " }")
                self.assertEqual(code, 1)
                self.assertIsNone(config)

    def test_real_ip_and_literal_redirect_preview(self):
        code, _, config = self.run_preview("example.org",
            'location / { proxy_pass http://127.0.0.1:2010; } location = /old { return 308 /new; }',
            'set_real_ip_from 10.0.0.0/8; set_real_ip_from 2001:db8::/32; '
            'real_ip_header X-Forwarded-For; real_ip_recursive on;')
        self.assertEqual(code, 0)
        self.assertEqual(config["proxy"]["real_ip"]["trusted_proxies"], ["10.0.0.0/8", "2001:db8::/32"])
        self.assertTrue(config["proxy"]["real_ip"]["recursive"])
        redirects = [route for route in config["proxy"]["routes"] if "action" in route]
        self.assertTrue(all(route["action"]["status"] == 308 and not route["upstreams"] for route in redirects))

    def test_literal_tls_and_mandatory_client_auth_preview(self):
        files = ("ssl_certificate /etc/tls/server.pem; ssl_certificate_key /etc/tls/key.pem; "
                 "ssl_protocols TLSv1.3;")
        location = "location / { proxy_pass http://127.0.0.1:2010; }"
        auth = "ssl_verify_client on; ssl_client_certificate /etc/tls/clients.pem;"
        code, _, config = self.run_preview("api.example.org", location, files + auth, "127.0.0.1:8443 ssl")
        self.assertEqual(code, 0)
        self.assertEqual(config["proxy"]["tls"], {
            "cert_file": "/etc/tls/server.pem", "key_file": "/etc/tls/key.pem",
            "versions": ["tls1.3"], "client_ca_file": "/etc/tls/clients.pem",
        })
        for options, listener in (
            (files + auth, "127.0.0.1:8443"),
            (files + auth.replace(" on;", " optional;"), "127.0.0.1:8443 ssl"),
            (files.replace("TLSv1.3", "TLSv1.1"), "127.0.0.1:8443 ssl"),
        ):
            with self.subTest(options=options, listener=listener):
                code, _, config = self.run_preview("api.example.org", location, options, listener)
                self.assertEqual(code, 1)
                self.assertIsNone(config)

    def test_explicit_static_root_alias_and_direct_preview(self):
        code, report, config = self.run_preview("example.org",
            "location / { root /srv/web; index index.html home.html; try_files $uri $uri/ =404; } "
            "location /assets/ { alias /srv/assets/; } location = /probe { return 200 ready; }")
        self.assertEqual(code, 0)
        self.assertEqual(report["preview"]["confidence"], "manual_review")
        self.assertFalse(config["proxy"]["upstreams"])
        rows = {route["path_prefix"]:route["content"] for route in config["proxy"]["routes"]}
        self.assertEqual(rows["/"]["try_files"], ["$uri", "$uri/"])
        self.assertEqual(rows["/assets/"]["alias"], "/srv/assets/")
        self.assertEqual(rows["/probe"]["body"], "ready")
        for directive in ("root /srv/web; autoindex on;", "root /srv/web; try_files $uri @application;",
                          "alias /srv/assets;", "root $base;"):
            with self.subTest(directive=directive):
                code, _, config = self.run_preview("example.org", "location /static/ { " + directive + " }")
                self.assertEqual(code, 1)
                self.assertIsNone(config)

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
