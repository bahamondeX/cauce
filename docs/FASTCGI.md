# FastCGI gateway

Set an upstream to `protocol: fastcgi` and give its route an
`application` configuration. Use a TCP address or an absolute
`unix:/path` socket. Each endpoint uses an exclusive request lease,
the existing global and endpoint capacity limits, and passive failure
cooldown. HTTP and FastCGI connections have separate typed transports
and endpoint identities. [YAML example](../examples/fastcgi.yaml)
[TOML example](../examples/fastcgi.toml).

The route selects a fixed script relative to an absolute document root.
`script_name` specifies the CGI URL path independently. A request
path, query parameter, or request header cannot change the selected file.
Deploy the script and any symlinks under a trusted PHP-FPM document root.
Cauce passes the validated request method, query, original authority,
canonical client IP, content metadata and TLS status in CGI variables.
HTTP fields use the `HTTP_` prefix and the proxy-specific environment
variable is excluded. Names with underscores are omitted to prevent
aliases during conversion.

Known-length bodies stream in bounded FastCGI records. Set
`request_buffer_bytes` to buffer up to 8 MiB per request and accept
uploads whose length becomes known at end of stream. Buffering carries
a per-generation quota. With buffering disabled, an unknown-length
upload receives 411. Request trailers are rejected. The configured
upstream headers deadline includes the upload and response header
exchange.

Cauce validates STDOUT headers, status, body length, and END_REQUEST.
STDERR is discarded under a 64 KiB cap and its contents are excluded
from logs. Each FastCGI connection carries one request at a time.
`keep_connection: true` enables reuse after a successful upload and
completed response. Incomplete, malformed, cancelled and expired
exchanges close their sockets. Backend applications should finish
reading STDIN before emitting a large early response.

The PHP-FPM fixture compares POST, multipart, redirects, cookies,
large responses and error status with NGINX 1.30.8. The independent
wire fixture additionally exercises Unix sockets, malformed records,
bounded uploads and reload rejection. [FastCGI specification](https://fastcgi-archives.github.io/FastCGI_Specification.html)
[NGINX FastCGI module](https://nginx.org/en/docs/http/ngx_http_fastcgi_module.html).
