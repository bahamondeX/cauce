"""Per-module request data and independently rendered loopback configurations."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading

from tls_gateway import issue, openssl


def proxy(config, reference_port, resources, folder):
    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.headers.get("X-Tenant", "missing").encode()
            self.send_response(404 if self.path == "/missing" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Hidden", "internal")
            self.send_header("X-Repeated", "first")
            self.send_header("X-Repeated", "second")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    backend = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Origin))
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    resources.callback(backend.shutdown)
    address = f"127.0.0.1:{backend.server_port}"
    config["upstreams"] = [{"id": "backend", "address": address, "host": "origin.test",
                            "isolation": "fixture", "connections": 4, "idle_connections": 2,
                            "failure_backoff_ms": 100}]
    config["routes"] = [{"id": "proxy", "host": "compat.test", "path_prefix": "/", "upstreams": ["backend"],
                         "headers": {"request": {"set": {"X-Tenant": "fixture"}},
                                     "response": {"remove": ["X-Hidden"]}}}]
    tls_locations, tls_cases = proxy_sni(config, resources, folder)
    reference = f"""server {{ listen 127.0.0.1:{reference_port}; server_name compat.test;
    location / {{ proxy_pass http://{address}; proxy_set_header X-Tenant fixture;
                  proxy_hide_header X-Hidden; }}
    {tls_locations} }}"""
    return reference, [
        {"name": "override-client-header-and-hide-response-header", "target": "/", "status": 200,
         "request_headers": {"X-Tenant": "client"}, "expected_body": "fixture",
         "headers": ["x-hidden", "x-repeated", "content-length"]},
        {"name": "filter-upstream-error-and-retain-multiplicity", "target": "/missing", "status": 404,
         "expected_body": "fixture", "headers": ["x-hidden", "x-repeated", "content-length"]},
    ] + tls_cases, []


def proxy_sni(config, resources, folder):
    class TlsOrigin(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (self.connection.fixture_sni or "no-sni").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    certificates = folder / "origin-tls"
    certificates.mkdir(parents=True)
    openssl(certificates, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=compatibility-ca", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash",
            "-keyout", "ca-key.pem", "-out", "ca.pem")
    issue(certificates, 1, "backend", "ca", "serverAuth", "backend.test")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificates / "backend.pem", certificates / "backend-key.pem")
    tls.set_servername_callback(lambda sock, name, _: setattr(sock, "fixture_sni", name))
    backend = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), TlsOrigin))
    backend.socket = tls.wrap_socket(backend.socket, server_side=True)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    resources.callback(backend.shutdown)
    address = f"127.0.0.1:{backend.server_port}"
    ca = str(certificates / "ca.pem")
    locations, cases = [], []
    for suffix, tls_name, sni in (("on", "backend.test", True), ("off", "backend.test", False),
                                  ("invalid-name", "wrong.test", False)):
        name = f"sni-{suffix}"
        path = f"/{name}"
        option = "on" if sni else "off"
        locations.append(f"""location = {path} {{ proxy_pass https://{address};
          proxy_ssl_name {tls_name}; proxy_ssl_trusted_certificate {json.dumps(ca)};
          proxy_ssl_verify on; proxy_ssl_server_name {option}; }}""")
        config["upstreams"].append({"id": name, "address": address, "host": "backend.test",
                                  "isolation": "fixture", "connections": 4, "idle_connections": 2,
                                  "failure_backoff_ms": 100,
                                  "tls": {"server_name": tls_name, "ca_file": ca, "send_sni": sni}})
        config["routes"].append({"id": name, "host": "compat.test", "path_match": "exact",
                                 "path_prefix": path, "upstreams": [name]})
        if suffix == "invalid-name":
            cases.append({"name": name, "target": path, "status": 502, "compare_body": False})
        else:
            cases.append({"name": name, "target": path, "status": 200,
                          "expected_body": tls_name if sni else "no-sni"})
    return "\n".join(locations), cases


def nested(config, reference_port, _resources, _folder):
    config["routes"] = []

    def route(id_, selector, literal, body, parent=None, scope=None):
        row = {"id": id_, "host": "compat.test", "path_match": selector,
               "content": {"type": "direct", "status": 200, "body": body}}
        row["path_pattern" if selector.startswith("regex") else "path_prefix"] = literal
        if parent:
            row["parent"] = parent
        if scope:
            row["headers"] = {"response": {"set": {"X-Scope": scope}}}
        config["routes"].append(row)

    route("root", "literal_prefix", "/", "root")
    route("nested-exact", "exact", "/nest/exact.outer", "nested-exact", "outer")
    route("outer", "literal_prefix", "/nest", "outer", scope="inherited")
    route("deep", "literal_prefix", "/nest/deep", "deep", "outer", "overridden")
    route("deep-exact", "exact", "/nest/deep/fixed", "deep-exact", "deep")
    route("quiet", "prefix_no_regex", "/nest/quiet", "quiet", "outer")
    route("local-regex", "regex", "[.]near$", "local-regex", "outer")
    route("regex-parent", "regex", "/reg/", "regex-parent")
    route("regex-child", "regex", "^/reg/exact$", "regex-child", "regex-parent")
    route("top-regex", "regex", "[.](near|outer)$", "top-regex")
    route("protected", "prefix_no_regex", "/shield", "protected")
    route("protected-child", "literal_prefix", "/shield/child", "protected-child", "protected")

    reference = f"""server {{ listen 127.0.0.1:{reference_port}; server_name compat.test;
      location / {{ return 200 root; }}
      location /nest {{
        add_header X-Scope inherited;
        location = /nest/exact.outer {{ return 200 nested-exact; }}
        location /nest/deep {{
          add_header X-Scope overridden;
          location = /nest/deep/fixed {{ return 200 deep-exact; }}
          return 200 deep;
        }}
        location ^~ /nest/quiet {{ return 200 quiet; }}
        location ~ [.]near$ {{ return 200 local-regex; }}
        return 200 outer;
      }}
      location ~ /reg/ {{
        location ~ ^/reg/exact$ {{ return 200 regex-child; }}
        return 200 regex-parent;
      }}
      location ~ [.](near|outer)$ {{ return 200 top-regex; }}
      location ^~ /shield {{
        location /shield/child {{ return 200 protected-child; }}
        return 200 protected;
      }}
    }}"""
    checks = [
        ("prefix-default", "/nest", "outer", "inherited"),
        ("literal-prefix-without-segment-boundary", "/nested", "outer", "inherited"),
        ("longest-child-and-header-override", "/nest/deep/relative", "deep", "overridden"),
        ("grandchild-inherits-nearest-override", "/nest/deep/fixed", "deep-exact", "overridden"),
        ("nested-exact-beats-server-regex", "/nest/exact.outer?fixture=1", "nested-exact", "inherited"),
        ("nested-regex-beats-server-regex", "/nest/file.near", "local-regex", "inherited"),
        ("nested-no-regex-suppresses-same-depth-regex", "/nest/quiet/file.near", "top-regex", None),
        ("server-regex-can-override-nested-prefix", "/nest/deep/file.outer", "top-regex", None),
        ("server-no-regex-protects-included-child", "/shield/child/file.outer", "protected-child", None),
        ("nested-location-below-regex", "/reg/exact", "regex-child", None),
        ("regex-parent-fallback", "/reg/fallback", "regex-parent", None),
        ("nested-regex-out-of-scope", "/other/file.near", "top-regex", None),
        ("unrelated-fallback", "/other", "root", None),
    ]
    accepted = [
        {"name": label, "target": path, "status": 200, "expected_body": body,
         "headers": ["x-scope"], "expected_headers": {"x-scope": [scope] if scope else []}}
        for label, path, body, scope in checks
    ]
    parent = config["routes"][2]
    exact = config["routes"][1]
    rejected = [
        {"name": "outside-enclosing-path", "routes": [parent, {**exact, "path_prefix": "/outside"}],
         "reference": f"server {{ listen 127.0.0.1:{reference_port}; location /nest {{ location = /outside {{ return 200 bad; }} }} }}",
         "error": {"cauce": "outside its parent", "nginx": "is outside location"}},
        {"name": "exact-parent-forbidden", "routes": [{**parent, "path_match": "exact"}, exact],
         "reference": f"server {{ listen 127.0.0.1:{reference_port}; location = /nest {{ location /nest/child {{ return 200 bad; }} }} }}",
         "error": {"cauce": "invalid parent location", "nginx": "cannot be inside the exact location"}},
        {"name": "cross-authority-parent", "routes": [parent, {**exact, "host": "other.test"}],
         "error": {"cauce": "invalid parent location or host"}},
        {"name": "literal-location-below-regex-is-rejected-by-cauce",
         "routes": [config["routes"][7], {**exact, "parent": "regex-parent", "path_prefix": "/reg/exact"}],
         "error": {"cauce": "literal location below a regex parent is unsupported"}},
        {"name": "cycle-in-location-graph",
         "routes": [{**parent, "parent": "nested-exact"},
                    {**exact, "path_prefix": "/nest", "path_match": "literal_prefix"}],
         "error": {"cauce": "cyclic or excessively nested location"}},
    ]
    return reference, accepted, rejected


def header_filter(config, reference_port, _resources, _folder):
    def direct(name, path, status, parent=None, additions=None):
        route = {"id": name, "host": "compat.test", "path_prefix": path,
                 "path_match": "literal_prefix" if parent is None else "exact",
                 "content": {"type": "direct", "status": status,
                             "body": "" if status == 204 else name}}
        if parent:
            route["parent"] = parent
        if additions is not None:
            route["headers"] = {"response_add": additions}
        return route

    fields = [{"name": "X-Result", "value": "first"}, {"name": "X-Result", "value": "second"},
              {"name": "X-Empty", "value": ""}, {"name": "X-Always", "value": "present", "always": True}]
    root = direct("root", "/status", 200, additions=fields)
    config["routes"] = [root, direct("ok", "/status/ok", 200, "root"),
                        direct("missing", "/status/missing", 404, "root"),
                        direct("no-content", "/status/no-content", 204, "root"),
                        direct("disabled", "/status/disabled", 200, "root", []),
                        direct("local", "/status/local", 200, "root", [{"name": "X-Result", "value": "local"}])]
    reference = f"""server {{ listen 127.0.0.1:{reference_port}; server_name compat.test;
      location /status {{
        add_header X-Result first;
        add_header X-Result second;
        add_header X-Empty "";
        add_header X-Always present always;
        location = /status/ok {{ return 200 ok; }}
        location = /status/missing {{ return 404 missing; }}
        location = /status/no-content {{ return 204; }}
        location = /status/disabled {{ add_header_inherit off; return 200 disabled; }}
        location = /status/local {{ add_header X-Result local; return 200 local; }}
        return 200 root;
      }}
    }}"""
    checks = [("ok", 200, ["first", "second"], ["present"]), ("missing", 404, [], ["present"]),
              ("no-content", 204, ["first", "second"], ["present"]),
              ("disabled", 200, [], []), ("local", 200, ["local"], [])]
    accepted = [{"name": f"literal-response-headers-{name}", "target": f"/status/{name}", "status": status,
                 "headers": ["x-result", "x-always", "x-empty"],
                 "expected_headers": {"x-result": values, "x-always": always, "x-empty": []},
                 "expected_body": "" if status == 204 else name} for name, status, values, always in checks]
    rejected = [
        {"name": "append-a-proxy-owned-header",
         "routes": [direct("bad", "/status", 200, additions=[{"name": "Content-Length", "value": "7"}])],
         "error": {"cauce": "invalid YAML configuration field, type or quantity"}},
        {"name": "response-header-byte-budget",
         "routes": [direct("bad", "/status", 200, additions=[{"name": "X-Large", "value": "a" * 4096}])],
         "error": {"cauce": "response header policy exceeds its edit or byte budget"}},
    ]
    return reference, accepted, rejected


MODULES = {"ngx_http_core_module": nested, "ngx_http_headers_filter_module": header_filter,
           "ngx_http_proxy_module": proxy}
