# SCGI gateway

Set `protocol: scgi`, `idle_connections: 0`, and a route `application` object. TCP and Unix sockets are supported. Each request owns its connection until the response ends or the exchange is cancelled.

The adapter emits a netstring containing NUL-separated pairs, starting with `CONTENT_LENGTH` and `SCGI=1`, followed by exactly the declared body length. The environment limit is 32 KiB. A declared length streams directly. Body framing that lacks a usable length requires explicit `request_buffer_bytes` or receives HTTP 411. The memory limit for buffered requests also applies across the generation. SCGI receives raw body bytes.

The response parser accepts CGI headers and an optional HTTP status line. It verifies any declared content length and treats EOF as the boundary for responses without a length. Shared HTTP route policy, cache and compression run on the resulting response.

The test matrix compares the same native SCGI server and WSGI application through Cauce and NGINX 1.30.8. The Unix fixture independently validates netstring framing and tests truncated or malformed response headers. See [the fixture](../tests/application/test_wsgi.py), [configuration](../examples/wsgi.yaml) and [the SCGI specification](https://python.ca/scgi/protocol.txt).
