#!/usr/bin/env python3
"""Pin NGINX comparison to shared static and gzip behaviors on real sockets."""
import argparse
from email.parser import BytesParser
from email.policy import default
import gzip
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from content_handlers import request, settings, wait
from integration import port


def multipart(headers, payload):
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + headers["content-type"].encode() + b"\r\n\r\n" + payload
    )
    return [(part["content-range"], part.get_payload(decode=True)) for part in message.iter_parts()]


def compare(cauce, nginx):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary).resolve()
        root = directory / "web"
        root.mkdir()
        (root / "item.txt").write_bytes(b"0123456789")
        (root / "compress.txt").write_bytes(b"repeated data\n" * 4096)
        (root / "index.html").write_text("index")
        (root / "tried").mkdir()
        (root / "tried" / "yes.txt").write_text("found")
        cp, np, admin = port(), port(), port()
        config = settings()
        def route(id_, location, **fields):
            return dict(id=id_, host="localhost", path_prefix=location, path_match="literal_prefix", **fields)
        config.update(listen=f"127.0.0.1:{cp}", admin=f"127.0.0.1:{admin}", upstreams=[], routes=[
            route("site", "/", content=dict(type="static", root=str(root), index=["index.html"]),
                  compression=dict(algorithms=["gzip"], min_size=1024)),
            route("assets", "/assets/", content=dict(type="static", alias=str(root))),
            route("try", "/tried/", content=dict(type="static", root=str(root), try_files=["$uri", "$uri/"])),
            dict(id="return", host="localhost", path_prefix="/ready", path_match="exact",
                 content=dict(type="direct", status=200, body="ready", content_type="text/plain")),
        ])
        cauce_config = directory / "cauce.yaml"
        cauce_config.write_text(json.dumps(dict(version=1, proxy=config)))
        nginx_config = directory / "nginx.conf"
        nginx_config.write_text(f"""
          error_log stderr error;
          pid nginx.pid;
          worker_processes 1;
          events {{ worker_connections 128; }}
          http {{
            access_log off;
            types {{ text/plain txt; text/html html; }}
            client_body_temp_path client_body;
            proxy_temp_path proxy_temp;
            fastcgi_temp_path fastcgi_temp;
            scgi_temp_path scgi_temp;
            uwsgi_temp_path uwsgi_temp;
            server {{
              listen 127.0.0.1:{np};
              server_name localhost;
              absolute_redirect off;
              gzip on;
              gzip_comp_level 3;
              gzip_types text/plain;
              gzip_min_length 1024;
              gzip_vary on;
              location / {{ root "{root}"; index index.html; }}
              location /assets/ {{ alias "{root}/"; }}
              location /tried/ {{ root "{root}"; try_files $uri $uri/ =404; }}
              location = /ready {{ default_type text/plain; return 200 "ready"; }}
            }}
          }}
        """)
        stderr = directory / "cauce.stderr"
        nginx_stderr = directory / "nginx.stderr"
        with stderr.open("w") as error, nginx_stderr.open("w") as other:
            proxy = subprocess.Popen([cauce, cauce_config], cwd=directory, stderr=error)
            reference = subprocess.Popen([nginx, "-p", str(directory) + "/", "-c", str(nginx_config), "-g", "daemon off;"], cwd=directory, stderr=other)
        try:
            wait(stderr, '"started"', proxy)
            until = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", np), 0.2):
                        break
                except OSError:
                    if reference.poll() is not None or time.monotonic() >= until:
                        raise AssertionError(nginx_stderr.read_text())
                    time.sleep(0.025)
            initial = [request(port, "/item.txt") for port in (cp, np)]
            last_modified = initial[0][1]["last-modified"]
            assert last_modified == initial[1][1]["last-modified"], initial
            cases = [
                ("get", "GET", "/item.txt", [{}, {}], ["content-type", "content-length", "last-modified"]),
                ("head", "HEAD", "/item.txt", [{}, {}], ["content-type", "content-length"]),
                ("index", "GET", "/", [{}, {}], ["content-type", "content-length"]),
                ("alias", "GET", "/assets/item.txt", [{}, {}], ["content-length"]),
                ("static-try-found", "GET", "/tried/yes.txt", [{}, {}], ["content-type"]),
                ("static-try-missing", "GET", "/tried/no.txt", [{}, {}], []),
                ("direct", "GET", "/ready", [{}, {}], ["content-type"]),
                ("not-modified-etag", "GET", "/item.txt", [{"If-None-Match": item[1]["etag"]} for item in initial], []),
                ("not-modified-time", "GET", "/item.txt", [{"If-Modified-Since":last_modified}] * 2, []),
                ("single-range", "GET", "/item.txt", [{"Range":"bytes=2-4"}] * 2, ["content-range", "content-length"]),
                ("suffix-range", "GET", "/item.txt", [{"Range":"bytes=-3"}] * 2, ["content-range"]),
                ("unsatisfiable-range", "GET", "/item.txt", [{"Range":"bytes=200-300"}] * 2, ["content-range"]),
                ("multipart-range", "GET", "/item.txt", [{"Range":"bytes=0-1,8-9"}] * 2, []),
                ("if-range-date", "GET", "/item.txt", [{"Range":"bytes=4-6", "If-Range":last_modified}] * 2, ["content-range"]),
                ("gzip", "GET", "/compress.txt", [{"Accept-Encoding":"gzip"}] * 2, ["content-encoding", "content-type"]),
            ]
            for name, method, path, options, fields in cases:
                lhs, rhs = [request(side, path, method, headers) for side, headers in zip((cp, np), options)]
                assert lhs[0] == rhs[0], (name,lhs[:2],rhs[:2])
                for field in fields:
                    assert lhs[1].get(field) == rhs[1].get(field), (name,field,lhs[1],rhs[1])
                if name == "multipart-range":
                    assert multipart(lhs[1], lhs[2]) == multipart(rhs[1], rhs[2])
                elif name == "gzip":
                    assert gzip.decompress(lhs[2]) == gzip.decompress(rhs[2])
                    assert all("accept-encoding" in row[1]["vary"].lower() for row in (lhs,rhs))
                elif 200 <= lhs[0] < 300:
                    assert lhs[2] == rhs[2], name
                print(json.dumps({"case":name,"status":lhs[0],"shared_headers":fields,
                                  **({"coded_bytes":{"cauce":len(lhs[2]),"nginx":len(rhs[2])}} if name == "gzip" else {})}), flush=True)
        finally:
            for process in (proxy, reference):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=8)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cauce", type=Path)
    parser.add_argument("nginx", type=Path)
    arguments = parser.parse_args()
    compare(arguments.cauce.resolve(), arguments.nginx.resolve())
