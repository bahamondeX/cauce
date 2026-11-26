# Native uWSGI gateway

Set an upstream's `protocol: uwsgi` and set the route's `application` object. The upstream uses the normal endpoint selection, resource limits and transport connect timeout. Use a TCP address or a Unix socket and set `idle_connections: 0`. Cauce opens a new connection per request.

The adapter emits modifier 1, a little-endian 16-bit environment length, and modifier 2. Both modifiers default to zero. Each environment pair uses two length-prefixed fields. Cauce bounds the encoded environment at 32 KiB and each value at 16 KiB. It then writes the request body and reads the HTTP status line or CGI headers and body with downstream backpressure. Later requests use a separate socket, including after cancellation or malformed responses.

Use `application.script_name` to set the fixed WSGI mount. Cauce supplies the suffix as `PATH_INFO` on a segment boundary. It preserves the URL path encoding. Unknown-length requests require a bounded `request_buffer_bytes` setting or receive HTTP 411. Request trailers and response transfer codings are unsupported. Shared routing, cache, compression and response policy remain available.

The native suite runs independent Unix wire tests and the Debian uWSGI 2.0.21 Python plugin against the same WSGI app and NGINX 1.30.8. See [the fixture](../tests/application/test_wsgi.py), [configuration](../examples/wsgi.yaml) and [the uWSGI specification](https://uwsgi-docs.readthedocs.io/en/latest/Protocol.html).
