# Static files

Use `content.type: static` on a route. Set `root` to append the
complete effective URI path under the document directory. Set `alias`
to replace a literal location prefix with that directory. Select one of
these fields. The document directory must exist when the configuration
is loaded. The static handler runs after route actions, access control
and admission. A static-only process can configure an empty upstream
list. [Site example](../examples/content-and-cache.yaml).

`index` defaults to `[index.html]`. Directory listing is opt-in through
`listing`. Slash redirects preserve the query. Listings encode links and
escape filenames, and omit forbidden entries and symlinks.
`try_files` can test `$uri`, `$uri/` and literal filenames relative to the
document directory. This mode returns 404 when its candidates fail.
Use a route action for an explicit internal redirect.

GET and HEAD provide the selected representation's Content-Length and
MIME type. Metadata gives Last-Modified and, by default, a weak ETag.
`cache_control` supplies a response header and `expires_seconds` sets
an absolute Expires date from the current time. The handler supports
If-None-Match, If-Modified-Since, If-Match and If-Unmodified-Since.
Byte ranges include single, suffix and multipart ranges, with at most
eight ranges. If-Range accepts an exact Last-Modified date. Weak ETags
cannot satisfy If-Range or a non-wildcard If-Match. The static handler
uses a conservative 200 fallback for malformed, overlapping or excessive
range sets. Valid unsatisfiable ranges produce 416.

A configured directory descriptor anchors every lookup. Linux attempts
`openat2` with beneath-directory and no-link resolution flags. The
portable fallback walks bounded path components using no-follow
directory-relative opens. The file is validated and streamed from the
same descriptor. Hidden components and sensitive suffixes are denied by
default. Only regular files and directories can be served. Deploy document
directories with trusted writers and exclude confidential files and
hard links to them. Access root selection, suffix policy and route ACLs
are independent controls.

By default 32 concurrent static opens or streams fit per route.
`max_open` caps blocking opens, reads and retained descriptors. Reads use
64 KiB chunks and hold the permit through any outstanding blocking read.
Directory listings and scan work are independently bounded. Cancellation
and body deadlines release the resources. [Content architecture decision](adr/0013-content-and-cache.md).

The Hyper interface used here passes body frames through its connection
writer. Its public body interface carries bytes, not a file region and
downstream socket. Direct sendfile would require a dedicated plaintext
HTTP/1.1 writer with framing, lock ownership, timeout and backpressure
validation. TLS and HTTP/2 continue through their protocol stack.
Descriptor and metadata caching, readahead tuning and direct I/O
remain separate experiments.
