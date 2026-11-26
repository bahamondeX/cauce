"""Per-module request data and independently rendered loopback configurations."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


def proxy(config, reference_port, resources):
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
    reference = f"""server {{ listen 127.0.0.1:{reference_port}; server_name compat.test;
    location / {{ proxy_pass http://{address}; proxy_set_header X-Tenant fixture;
                  proxy_hide_header X-Hidden; }} }}"""
    return reference, [
        {"name": "override-client-header-and-hide-response-header", "target": "/", "status": 200,
         "request_headers": {"X-Tenant": "client"}, "expected_body": "fixture",
         "headers": ["x-hidden", "x-repeated", "content-length"]},
        {"name": "filter-upstream-error-and-retain-multiplicity", "target": "/missing", "status": 404,
         "expected_body": "fixture", "headers": ["x-hidden", "x-repeated", "content-length"]},
    ]


MODULES = {"ngx_http_proxy_module": proxy}
