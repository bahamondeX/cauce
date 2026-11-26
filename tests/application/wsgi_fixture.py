"""Independent WSGI application served by the independent uWSGI server."""
import hashlib
import json
import time


def application(environ, start):
    query = environ.get("QUERY_STRING", "")
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
