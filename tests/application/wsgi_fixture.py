"""Independent WSGI application served by the independent uWSGI server."""
import hashlib
import json
import time
from urllib.parse import parse_qs


def application(environ, start):
    query = environ.get("QUERY_STRING", "")
    if query.startswith("mode=benchmark&"):
        args = parse_qs(query)
        size = min(16 * 1024 * 1024, max(1, int(args.get("size", ["1"])[0])))
        hold = min(1000, max(0, int(args.get("hold", ["0"])[0])))
        remaining = int(environ.get("CONTENT_LENGTH") or 0)
        digest = hashlib.sha256()
        while remaining:
            chunk = environ["wsgi.input"].read(min(65536, remaining))
            if not chunk:
                raise ValueError("truncated request body")
            digest.update(chunk)
            remaining -= len(chunk)
        if hold:
            time.sleep(hold / 1000)
        start("200 OK", [("Content-Type", "application/octet-stream"), ("Content-Length", str(size)),
                         ("X-Request-SHA256", digest.hexdigest())])
        for offset in range(0, size, 65536):
            yield b"x" * min(65536, size - offset)
        return
    if query == "slow":
        start("200 OK", [("Content-Type", "application/octet-stream")])
        yield b"x" * 32_768
        time.sleep(0.5)
        yield b"y" * 32_768
        return
    if query == "redirect":
        start("307 Temporary Redirect", [("Location", "/next"), ("Content-Length", "0")])
        return
    body = environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))
    result = json.dumps({
        "path": environ.get("PATH_INFO"),
        "query": query,
        "method": environ["REQUEST_METHOD"],
        "length": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }, sort_keys=True).encode()
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(result)))]
    start("500 Internal Server Error" if query == "error" else "201 Created", headers)
    yield result
