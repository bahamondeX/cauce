#!/usr/bin/env python3
"""Differential HTTP gateway checks on loopback plus Cauce security cases."""
import argparse
from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from compare_nginx_routing import ROOT, port, verify


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/hold":
            self.server.held.set()
            self.server.release.wait(5)
        data = json.dumps([self.path, self.headers.get("X-Forwarded-For"), self.server.label]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def call(listen, path, chain=None, source=None, real=None):
    conn = http.client.HTTPConnection("127.0.0.1", listen, timeout=5, source_address=source)
    headers = {"Host": "gateway.test"}
    if chain is not None:
        headers["X-Forwarded-For"] = chain
    if real is not None:
        headers["X-Real-IP"] = real
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        data = response.read()
        return response.status, response.getheader("Location"), json.loads(data) if response.status == 200 else None
    finally:
        conn.close()


def route(name, path, *, action="", upstreams='["a"]', balance="round_robin", policy="", mode="exact"):
    selector = "path_pattern" if mode == "named" else "path_prefix"
    return f"""
[[routes]]
id="{name}"
host="gateway.test"
{selector}="{path}"
path_match="{mode}"
balance="{balance}"
upstreams={upstreams}
{action}
{policy}
"""


def fixtures(output, proxy, admin, nginx, a, b):
    base = (ROOT / "bench/increment7/configs/cauce-clear.toml").read_text().split("[[upstreams]]")[0]
    base = base.replace('0.0.0.0:8080', f'127.0.0.1:{proxy}').replace('127.0.0.1:9090', f'127.0.0.1:{admin}')
    source = base + """
[http2]
enabled=true
[real_ip]
trusted_proxies=["127.0.0.1/32","10.0.0.0/8","2001:db8:1::/48"]
recursive=true
[rate_zones.client]
key="client_ip"
max_keys=8
rate_per_second=1
reject_status=429
[rate_zones.active]
key="route"
max_keys=1
max_concurrent=1
reject_status=429
error_body="busy"
"""
    for name, origin, weight in (("a", a, 3), ("b", b, 1)):
        source += f"""
[[upstreams]]
id="{name}"
address="127.0.0.1:{origin}"
host="origin.test"
isolation="fixture"
connections=16
idle_connections=8
failure_backoff_ms=1000
weight={weight}
"""
    for name, path in (("identity", "/identity"), ("final", "/final/"), ("allowed", "/allowed")):
        source += route(name, path, mode="literal_prefix",
                        policy='allow_clients=["198.51.100.0/24"]' if name == "allowed" else "")
    source += route("old", "/old/", mode="literal_prefix",
                    action='action={kind="rewrite",pattern="^/old/(.*)$",target="/final/$1?new=1"}')
    source += route("old-redirect", "/redirect",
                    action='action={kind="redirect",status=308,target="/final/",query="replace"}', upstreams="[]")
    source += route("internal", "/internal",
                    action='action={kind="named",location="fallback"}', upstreams="[]")
    source += route("named", "fallback", mode="named")
    source += route("loop", "/loop", action='action={kind="rewrite",target="/loop"}', upstreams="[]")
    source += route("limited", "/limited", policy='rate_zone="client"')
    source += route("limited-other", "/limited-other", policy='rate_zone="client"')
    source += route("hold", "/hold", policy='rate_zone="active"')
    source += route("weighted", "/weighted", balance="weighted_round_robin", upstreams='["a","b"]')
    source += route("hashed", "/hashed", balance="consistent_hash", upstreams='["a","b"]')
    (output/"cauce.toml").write_text(source)
    temp_paths = " ".join(f"{name}_temp_path {name};" for name in ("client_body","proxy","fastcgi","uwsgi","scgi"))
    (output/"nginx.conf").write_text(f"""
daemon off; master_process off; pid fixture.pid; error_log stderr error;
events {{ }}
http {{
    access_log off; {temp_paths}
    limit_req_zone $binary_remote_addr zone=client:1m rate=1r/s;
    upstream weighted {{ server 127.0.0.1:{a} weight=3; server 127.0.0.1:{b}; }}
    server {{
        listen 127.0.0.1:{nginx}; server_name gateway.test;
        set_real_ip_from 127.0.0.1/32; set_real_ip_from 10.0.0.0/8; set_real_ip_from 2001:db8:1::/48;
        real_ip_header X-Forwarded-For; real_ip_recursive on;
        absolute_redirect off; proxy_set_header X-Forwarded-For $remote_addr;
        location = /identity-real {{ real_ip_header X-Real-IP; proxy_pass http://127.0.0.1:{a}; }}
        location = /identity {{ proxy_pass http://127.0.0.1:{a}; }}
        location /final/ {{ proxy_pass http://127.0.0.1:{a}; }}
        location = /allowed {{ allow 198.51.100.0/24; deny all; proxy_pass http://127.0.0.1:{a}; }}
        location /old/ {{ rewrite ^/old/(.*)$ /final/$1?new=1 last; }}
        location = /redirect {{ return 308 /final/; }}
        location = /internal {{ error_page 418 = @fallback; return 418; }}
        location @fallback {{ proxy_pass http://127.0.0.1:{a}; }}
        location = /limited {{ limit_req zone=client; limit_req_status 429; proxy_pass http://127.0.0.1:{a}; }}
        location = /limited-other {{ limit_req zone=client; limit_req_status 429; proxy_pass http://127.0.0.1:{a}; }}
        location = /weighted {{ proxy_pass http://weighted; }}
    }}
}}
""")


def report(label, value):
    print(json.dumps({"check": label, "observed": value}), flush=True)


def h2_reload_check(child, log, listen, backend):
    node = shutil.which("node")
    if not node:
        return
    program = r"""
const h2 = require('node:http2');
const session = h2.connect('http://127.0.0.1:' + process.argv[1]);
session.setTimeout(5000, () => session.destroy());
const request = (path) => new Promise((resolve, reject) => {
  const req = session.request({':authority':'gateway.test', ':path':path});
  let status;
  req.on('response', headers => { status = headers[':status']; });
  req.on('data', () => {});
  req.on('end', () => resolve(status));
  req.on('error', reject);
  req.end();
});
const first = request('/hold');
process.stdin.once('data', async () => {
  console.log(JSON.stringify(await request('/hold')));
  console.log(JSON.stringify(await first));
  session.close();
  process.stdin.pause();
});
"""
    worker = subprocess.Popen([node, "-e", program, str(listen)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert backend.held.wait(3), "first HTTP/2 stream never reached origin"
        child.send_signal(signal.SIGHUP)
        for _ in range(150):
            if '"reloaded"' in log.read_text():
                break
            time.sleep(.02)
        else:
            raise AssertionError("gateway did not reload")
        worker.stdin.write("continue\n")
        worker.stdin.flush()
        second = int(worker.stdout.readline())
        assert second == 429, second
        backend.release.set()
        first = int(worker.stdout.readline())
        assert first == 200, first
        assert worker.wait(timeout=5) == 0
        report("http2-concurrent-admission-survives-reload", [first, second])
    finally:
        backend.release.set()
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cauce", type=Path)
    parser.add_argument("nginx", type=Path)
    args = parser.parse_args()
    with ExitStack() as stack:
        output = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        a, b = [stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Origin)) for _ in range(2)]
        for label, origin in (("a",a),("b",b)):
            origin.label = label
            origin.held = threading.Event()
            origin.release = threading.Event()
            threading.Thread(target=origin.serve_forever, daemon=True).start()
            stack.callback(origin.shutdown)
        proxy, admin, nginx = port(), port(), port()
        fixtures(output, proxy, admin, nginx, a.server_port, b.server_port)
        path = output/"proxy.log"
        log = stack.enter_context(path.open("w"))
        children = [
            subprocess.Popen([str(args.cauce.resolve()), str(output/"cauce.toml")], stderr=log),
            subprocess.Popen([str(args.nginx.resolve()), "-p", str(output)+"/", "-c", "nginx.conf", "-e", "stderr"]),
        ]
        for child in children:
            stack.callback(child.wait, 8)
            stack.callback(child.terminate)
        for child, listen in zip(children, (proxy, nginx)):
            verify(child, lambda: call(listen, "/identity"))
        shared = [
            ("recursive-ipv4", "/identity", "192.0.2.71, 198.51.100.42, 10.1.2.3", (200, None, ["/identity", "198.51.100.42", "a"])),
            ("recursive-ipv6", "/identity", "2001:db8:2::42, 2001:db8:1::42", (200, None, ["/identity", "2001:db8:2::42", "a"])),
            ("capture-and-query", "/old/alpha?keep=2", None, (200, None, ["/final/alpha?new=1&keep=2", "127.0.0.1", "a"])),
            ("redirect", "/redirect?a=1", None, (308, "/final/", None)),
            ("named-location", "/internal", None, (200, None, ["/internal", "127.0.0.1", "a"])),
            ("access-denial", "/allowed", None, (403, None, None)),
            ("resolved-address-access", "/allowed", "198.51.100.42", (200, None, ["/allowed", "198.51.100.42", "a"])),
        ]
        for label, route_path, chain, expected in shared:
            assert call(proxy, route_path, chain) == call(nginx, route_path, chain) == expected, label
            report(label, expected)
        for listen in (proxy,nginx):
            statuses = [call(listen, route_path, "198.51.100.77")[0] for route_path in ("/limited","/limited-other")]
            assert statuses == [200,429], statuses
            targets = [call(listen,"/weighted")[2][-1] for _ in range(20)]
            assert targets.count("a") == 15 and targets.count("b") == 5, targets
        report("shared-zone-and-weight-ratio", {"rate":[200,429],"weighted":[15,5]})
        assert call(proxy, "/identity", "garbage, 198.51.100.42")[2][1] == "127.0.0.1"
        assert call(proxy, "/identity", "198.51.100.42", source=("127.0.0.2",0))[2][1] == "127.0.0.2"
        assert call(proxy, "/loop")[0] == 500
        assert b".42" not in path.read_bytes()
        first = call(proxy, "/hashed", "198.51.100.42")
        assert all(call(proxy,"/hashed","198.51.100.42") == first for _ in range(6))
        report("malformed-and-untrusted-forwarding-loop-and-hash","passed")
        h2_reload_check(children[0], path, proxy, a)
        config = output / "cauce.toml"
        original = config.read_text()
        previous = path.read_text().count('"reloaded"')
        config.write_text(original.replace("[real_ip]", '[real_ip]\nheader="x_real_ip"'))
        children[0].send_signal(signal.SIGHUP)
        for _ in range(150):
            if path.read_text().count('"reloaded"') > previous:
                break
            time.sleep(.02)
        else:
            raise AssertionError("real-IP header policy reload did not publish")
        expected = (200,None,["/identity-real","2001:db8:2::42","a"])
        assert call(proxy,"/identity-real", real="2001:db8:2::42") == call(nginx,"/identity-real", real="2001:db8:2::42") == expected
        report("selected-x-real-ip", expected)

        spare, admin_spare = port(), port()
        connection_config = output / "socket-limit.toml"
        connection_config.write_text(("max_connections_per_peer=1\n"+original)
            .replace(f":{proxy}", f":{spare}", 1).replace(f":{admin}", f":{admin_spare}", 1))
        peer_limited = subprocess.Popen([str(args.cauce.resolve()), str(connection_config)], stderr=log)
        stack.callback(peer_limited.wait, 8)
        stack.callback(peer_limited.terminate)
        verify(peer_limited, lambda: call(spare, "/identity"))
        holder = http.client.HTTPConnection("127.0.0.1", spare, timeout=3)
        holder.request("GET", "/identity", headers={"Host":"gateway.test"})
        assert holder.getresponse().read()
        try:
            call(spare, "/identity")
        except (OSError, http.client.HTTPException):
            pass
        else:
            raise AssertionError("same-peer TCP cap allowed a second socket")
        holder.close()
        for _ in range(150):
            try:
                assert call(spare, "/identity")[0] == 200
                break
            except (OSError, http.client.HTTPException):
                time.sleep(.02)
        else:
            raise AssertionError("same-peer TCP permit did not recover")
        report("physical-peer-tcp-admission", "passed")


if __name__ == "__main__":
    main()
