import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/nginx-migration-report.py"
BINARY = Path(os.environ.get("CAUCE_TEST_BINARY", ROOT / "target/debug/cauce")).resolve()


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
                    and BINARY.is_file()):
                validation = subprocess.run([str(BINARY), "check", str(destination)],
                                            capture_output=True, check=False)
                self.assertEqual(validation.returncode, 0, validation.stderr)
            return result.returncode, report, config

    def test_include_expansion_provenance_and_validated_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            include = root / "locations"
            include.mkdir()
            source = root / "nginx.conf"
            destination = root / "cauce.yaml"
            source.write_text("events {} http { set_real_ip_from 127.0.0.1; "
                              "server { listen 127.0.0.1:8080; server_name app.test; "
                              "include locations/*.conf; } }")
            file = include / "01.conf"
            file.write_text("location / { proxy_pass http://127.0.0.1:8000; }")
            args = [sys.executable, str(TOOL), str(source), "--include-root", str(root),
                    "--preview", str(destination), "--validate-bin", str(BINARY)]
            result = subprocess.run(args, capture_output=True, text=True, check=False)
            report = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, report)
            self.assertTrue(report["preview"]["validated"])
            self.assertEqual(report["includes"][0]["files"], ["locations/01.conf"])
            self.assertEqual(next(item for item in report["directives"] if item["directive"] == "proxy_pass")["source"],
                             "locations/01.conf")
            self.assertEqual(yaml.safe_load(destination.read_text())["proxy"]["real_ip"]["trusted_proxies"],
                             ["127.0.0.1/32"])
            destination.unlink()
            file.write_text("location / { proxy_pass http://$backend; }")
            rejected = subprocess.run(args, capture_output=True, text=True, check=False)
            report = json.loads(rejected.stdout)
            self.assertEqual(rejected.returncode, 1)
            self.assertFalse(destination.exists())
            self.assertEqual(report["variable_uses"][0]["variables"], ["backend"])

    def test_include_cycle_and_root_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "allowed"
            root.mkdir()
            source = root / "nginx.conf"
            args = [sys.executable, str(TOOL), str(source), "--include-root", str(root)]
            source.write_text("include nginx.conf;")
            cycle = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(cycle.returncode, 2)
            self.assertIn("include cycle", cycle.stdout)
            outside = root.parent / "outside.conf"
            outside.write_text("secret-content")
            (root / "outside.conf").symlink_to(outside)
            source.write_text("include outside.conf;")
            escape = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(escape.returncode, 2)
            self.assertIn("include outside root", escape.stdout)
            self.assertNotIn("secret-content", escape.stdout)

    def test_application_and_mail_recognition_require_manual_mapping(self):
        for protocol in ("fastcgi", "uwsgi", "scgi"):
            code, report, config = self.run_preview("example.org",
                f"location / {{ {protocol}_pass 127.0.0.1:2000; }}")
            self.assertEqual(code, 1)
            self.assertIsNone(config)
            item = next(row for row in report["directives"] if row["directive"] == f"{protocol}_pass")
            self.assertEqual(item["status"], "manual_review")
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = (Path(tmp) / f for f in ("nginx.conf", "preview.yaml"))
            source.write_text("mail { auth_http http://127.0.0.1:8081/secret; "
                              "server { protocol smtp; starttls only; listen 127.0.0.1:1587; } }")
            result = subprocess.run([sys.executable, str(TOOL), str(source), "--preview",
                                     str(destination)], capture_output=True, check=False, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(destination.exists())
            self.assertNotIn("/secret", result.stdout)
            report = json.loads(result.stdout)
            items = {row["directive"]: row["status"] for row in report["directives"]}
            self.assertEqual(items["mail"], "structure")
            self.assertEqual(items["auth_http"], "manual_review")

    def test_stream_preview_validates_literal_listeners_and_requires_proxy_trust(self):
        template = """
        stream {
            server {
                listen 127.0.0.1:15432 proxy_protocol;
                set_real_ip_from 127.0.0.1/32;
                proxy_protocol on;
                proxy_half_close on;
                proxy_pass unix:/tmp/fixture.sock;
            }
            server {
                listen [::1]:1053 udp;
                proxy_responses 1;
                proxy_pass [::1]:5353;
                proxy_timeout 5s;
            }
            server {
                listen 127.0.0.1:8443;
                ssl_preread on;
                proxy_pass 127.0.0.1:9443;
            }
        }
        """
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = (Path(tmp) / f for f in ("nginx.conf", "preview.yaml"))
            args = [sys.executable, str(TOOL), str(source), "--preview", str(destination)]
            source.write_text(template)
            result = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)
            stream = yaml.safe_load(destination.read_text())["proxy"]["stream"]
            self.assertEqual(stream["tcp"][0]["options"]["proxy_out"], "v1")
            self.assertEqual(stream["udp"][0]["options"]["idle_timeout"], "5s")
            self.assertEqual(stream["tls_passthrough"][0]["routes"], [])
            if BINARY.is_file():
                subprocess.run([str(BINARY), "check", str(destination)],
                               capture_output=True, check=True)
            destination.unlink()
            source.write_text(template.replace("set_real_ip_from 127.0.0.1/32;", ""))
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 1)
            self.assertFalse(destination.exists())

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

    def test_literal_nested_routes_preserve_authorities_and_header_inheritance(self):
        code, report, config = self.run_preview("example.org", """
            location /api {
                proxy_pass http://127.0.0.1:2010;
                proxy_set_header X-Tenant parent;
                location = /api/lookup {
                    proxy_pass http://127.0.0.1:2011;
                    proxy_hide_header X-Private;
                }
            }
        """)
        self.assertEqual(code, 0)
        routes = config["proxy"]["routes"]
        self.assertEqual(len(routes), 4)
        self.assertEqual(report["preview"]["minimum_distribution_profile"], "core")
        self.assertEqual({key for row in report["preview"]["route_mappings"] for key in row["target_routes"]},
                         {route["id"] for route in routes})
        parents = {route["id"]: route["host"] for route in routes if "parent" not in route}
        for child in [route for route in routes if "parent" in route]:
            self.assertEqual(child["host"], parents[child["parent"]])
            self.assertEqual(child["headers"], {"request": {"set": {"x-tenant": "parent"}},
                                                "response": {"remove": ["x-private"]}})
        for invalid in ("= /api", "/elsewhere"):
            code, _, config = self.run_preview("example.org", f"""
                location {invalid} {{
                    proxy_pass http://127.0.0.1:2010;
                    location /api/lookup {{ proxy_pass http://127.0.0.1:2011; }}
                }}
            """)
            self.assertEqual(code, 1)
            self.assertIsNone(config)

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

    def test_internal_preview_preserves_the_flag_and_rejects_invalid_arguments(self):
        code, _, config = self.run_preview("example.org", """
            location /private {
                internal;
                return 200 hidden;
                location /private/child { return 200 nested; }
            }
        """)
        self.assertEqual(code, 0)
        routes = config["proxy"]["routes"]
        self.assertTrue(all(row["internal"] is True for row in routes if row["path_prefix"] == "/private"))
        self.assertTrue(all("parent" in row for row in routes if row["path_prefix"] == "/private/child"))
        code, _, config = self.run_preview("example.org", "location / { internal off; return 200 ok; }")
        self.assertNotEqual(code, 0)
        self.assertIsNone(config)

    def test_response_additions_preview_preserves_status_modes_and_rejects_merge(self):
        code, _, config = self.run_preview("example.org", """
            location /api {
                add_header X-Result first;
                add_header X-Result second always;
                return 200 ok;
                location = /api/disabled { add_header_inherit off; return 200 ok; }
                location = /api/default { return 200 ok; }
            }
        """)
        self.assertEqual(code, 0)
        routes = {row["path_prefix"]: row for row in config["proxy"]["routes"]}
        self.assertEqual(routes["/api/disabled"]["headers"]["response_add"], [])
        self.assertEqual(routes["/api/default"]["headers"]["response_add"], [
            {"name": "x-result", "value": "first", "always": False},
            {"name": "x-result", "value": "second", "always": True},
        ])
        code, _, config = self.run_preview("example.org", "location / { add_header_inherit merge; return 200 ok; }")
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

    def test_deployment_fixtures_validate_or_report_blocking_directives(self):
        fixtures = ROOT / "tests/fixtures/nginx-migration"
        cases = {
            "api-gateway": (), "static-site": (), "stream-gateway": (),
            "php-fpm": ("fastcgi_param", "fastcgi_pass"),
            "python-application": ("uwsgi_pass", "scgi_pass"),
            "cache-proxy": ("proxy_cache_path", "proxy_cache_background_update"),
            "mail-proxy": ("auth_http", "starttls"),
        }
        for name, blocking in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = root / (name + ".conf")
                destination = root / "preview.yaml"
                source.write_text((fixtures / source.name).read_text().replace("/srv/www/cauce-fixture", str(root)))
                result = subprocess.run([sys.executable, str(TOOL), str(source), "--include-root", str(root),
                                         "--preview", str(destination), "--validate-bin", str(BINARY)],
                                        capture_output=True, check=False, text=True)
                report = json.loads(result.stdout)
                if blocking:
                    self.assertEqual(result.returncode, 1, report)
                    self.assertFalse(destination.exists())
                    for directive in blocking:
                        self.assertTrue(any(row["directive"] == directive and row["source"] == source.name
                                            and row["line"] > 0 and row["status"] != "structure"
                                            for row in report["directives"]), (directive, report))
                else:
                    self.assertEqual(result.returncode, 0, report)
                    self.assertTrue(report["preview"]["validated"])
                    self.assertTrue(destination.is_file())
