#!/usr/bin/env python3
"""Compare shared flat locations and virtual hosts over loopback sockets."""

import argparse
from contextlib import closing
from functools import partial
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
VHOSTS = {
    "*.test": [("/", "fallback-wildcard")],
    "*.example.test": [("/", "suffix"), ("= /health", "health")],
    "mail.*": [("/", "prefix")],
    "mail.internal.*": [("/", "long-prefix")],
    "api.example.test": [("= /only", "exact-host")],
    "locations.example.test": [
        ("/", "root"), ("/img", "literal"), ("^~ /private", "skip-regex"),
        ("/private/preview", "longer-prefix"), ("= /img/icon.png", "exact"),
        ("~ ^/img/.*[.]png$", "first-regex"), ("~* [.]png$", "caseless-regex"),
    ],
}
CASES = [
    ("tenant.example.test", "/", "suffix"),
    ("A.B.EXAMPLE.TEST", "/", "suffix"),
    ("tenant.example.test", "/health?probe=1", "health"),
    ("tenant.example.test", "/health/child", "suffix"),
    ("mail.example.test", "/", "suffix"),
    ("mail.internal.invalid", "/", "long-prefix"),
    ("mail.invalid", "/", "prefix"),
    ("api.example.test", "/only", "exact-host"),
    ("api.example.test", "/", None),
    ("example.test", "/", "fallback-wildcard"),
    ("unmatched.invalid", "/", None),
    ("locations.example.test", "/imgix", "literal"),
    ("locations.example.test", "/img/icon.png?x=1", "exact"),
    ("locations.example.test", "/img/one.png", "first-regex"),
    ("locations.example.test", "/img/one.PNG", "caseless-regex"),
    ("locations.example.test", "/private/a.png", "skip-regex"),
    ("locations.example.test", "/private/preview/a.png", "caseless-regex"),
    ("locations.example.test", "/private/preview/plain", "longer-prefix"),
    ("locations.example.test", "/outside", "root"),
]


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        filtered = self.path == "/policy"
        payload = (json.dumps([self.headers.get("X-Tenant"), self.headers.get("X-Remove")]).encode()
                   if filtered else self.headers["Host"].encode())
        self.send_response(200)
        if filtered:
            self.send_header("X-Origin-Debug", "internal")
            self.send_header("X-Policy", "upstream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(listen, host, path):
    with closing(http.client.HTTPConnection("127.0.0.1", listen, timeout=3)) as conn:
        conn.request("GET", path, headers={"Host": host})
        response = conn.getresponse()
        content = response.read()
    return (response.status, content.decode() if response.status == 200 else None)


def check_policy(listen):
    with closing(http.client.HTTPConnection("127.0.0.1", listen, timeout=3)) as conn:
        conn.request("GET", "/policy", headers={
            "Host": "headers.example.test", "X-Tenant": "client", "X-Remove": "client",
        })
        response = conn.getresponse()
        return {"status": response.status, "request": json.loads(response.read()),
                "replacement": response.headers.get_all("X-Policy"),
                "removed": response.headers.get_all("X-Origin-Debug")}


def configure(work, cauce_port, nginx_port, backend):
    template = (ROOT / "bench/increment7/configs/cauce-clear.toml").read_text()
    template = (template.split("[[upstreams]]", 1)[0]
                .replace("0.0.0.0:8080", f"127.0.0.1:{cauce_port}")
                .replace("127.0.0.1:9090", f"127.0.0.1:{port()}"))
    nginx_http = [f"server {{ listen 127.0.0.1:{nginx_port} default_server; return 404; }}"]
    idx = 0
    for name, routes in VHOSTS.items():
        locations = []
        for location, backend_host in routes:
            idx += 1
            modifier, path = location.split(" ", 1) if " " in location else ("", location)
            mode = {"": "literal_prefix", "=": "exact", "^~": "prefix_no_regex",
                    "~": "regex", "~*": "regex_insensitive"}[modifier]
            selector = "path_pattern" if mode.startswith("regex") else "path_prefix"
            template += f"""
[[upstreams]]
id = "target{idx}"
address = "127.0.0.1:{backend}"
host = "{backend_host}"
isolation = "routing-test"
connections = 4
idle_connections = 2
failure_backoff_ms = 100

[[routes]]
id = "route{idx}"
host = "{name}"
{selector} = {json.dumps(path)}
path_match = "{mode}"
balance = "round_robin"
upstreams = ["target{idx}"]
"""
            locations.append(f'location {location} {{ return 200 "{backend_host}"; }}')
        if not any(location == "/" for location, _ in routes):
            locations.append("location / { return 404; }")
        nginx_http.append(f"server {{ listen 127.0.0.1:{nginx_port}; server_name {name}; "
                          + " ".join(locations) + " }")
    template += f"""
[[upstreams]]
id = "header-origin"
address = "127.0.0.1:{backend}"
host = "origin.test"
isolation = "routing-test"
connections = 4
idle_connections = 2
failure_backoff_ms = 100

[[routes]]
id = "header-policy"
host = "headers.example.test"
path_prefix = "/policy"
path_match = "exact"
balance = "round_robin"
upstreams = ["header-origin"]
[routes.headers.request]
remove = ["x-remove"]
[routes.headers.request.set]
x-tenant = "configured"
[routes.headers.response]
remove = ["x-origin-debug"]
[routes.headers.response.set]
x-policy = "configured"
"""
    nginx_http.append(f"""
server {{ listen 127.0.0.1:{nginx_port}; server_name headers.example.test;
    location = /policy {{
        proxy_pass http://127.0.0.1:{backend};
        proxy_http_version 1.1;
        proxy_set_header X-Tenant configured;
        proxy_set_header X-Remove "";
        proxy_hide_header X-Origin-Debug;
        proxy_hide_header X-Policy;
        add_header X-Policy configured always;
    }}
}}
""")
    (work / "cauce.toml").write_text(template)
    env = " ".join(f"{name}_temp_path {name};"
                   for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
    (work / "nginx.conf").write_text("daemon off; master_process off; pid nginx.pid; "
                                    "error_log stderr error; events {} http { access_log off; "
                                    + env + " ".join(nginx_http) + " }")


def verify(child, probe):
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError(f"fixture exited {child.returncode}")
        try:
            probe()
            return
        except (OSError, http.client.HTTPException):
            time.sleep(.03)
    raise RuntimeError("fixture failed to start")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cauce", type=Path)
    parser.add_argument("nginx", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Origin) as backend:
        work = Path(tmp)
        cauce_port, nginx_port = port(), port()
        configure(work, cauce_port, nginx_port, backend.server_port)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        with subprocess.Popen([str(args.cauce.resolve()), str(work / "cauce.toml")]) as cauce, \
                subprocess.Popen([str(args.nginx.resolve()), "-p", tmp + "/", "-c", "nginx.conf",
                                  "-e", "stderr"]) as nginx:
            try:
                verify(cauce, partial(request, cauce_port, "tenant.example.test", "/"))
                verify(nginx, partial(request, nginx_port, "tenant.example.test", "/"))
                for host, path, expected in CASES:
                    wanted = (200 if expected else 404, expected)
                    actual = request(cauce_port, host, path)
                    reference = request(nginx_port, host, path)
                    assert actual == reference == wanted, (host, path, actual, reference)
                    print(json.dumps({"host": host, "path": path, "status": actual[0], "marker": actual[1]}))
                actual, reference = check_policy(cauce_port), check_policy(nginx_port)
                assert actual == reference == {"status": 200, "request": ["configured", None],
                                               "replacement": ["configured"], "removed": None}
                print(json.dumps({"host": "headers.example.test", "path": "/policy", **actual}))
            finally:
                cauce.terminate()
                nginx.terminate()
                backend.shutdown()


if __name__ == "__main__":
    main()
