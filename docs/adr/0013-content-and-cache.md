# ADR 0013 · Typed content handling and bounded HTTP caching

Local content is a compiled static or direct handler on a route.
Existing route actions implement internal redirects before final
admission. Reverse proxying retains the existing transport path.
A proxy route can compile a cache policy and allocate a generation-local
store. Unconfigured local handlers, caches and compression have no
per-request allocation or synchronization in their disabled branches.

Static delivery opens a directory capability at configuration time.
File bodies produce bounded positional reads. The request slot and
file permit cover the stream and any outstanding read respectively.
A local completion body accounts for deadlines, terminal outcomes and
response bytes using the existing JSONL schema.

The cache separates RFC policy from its storage and flight state.
Moka provides a concurrent weighted cache and per-key initialization.
For this increment the store needs synchronous publication, invalidation
and hard aggregate admission for streamed fills. A route-local index
with FIFO eviction uses one short critical section and avoids holding
a lock over transport or filesystem work. A watch channel wakes
followers after publication or cancellation. No readiness promise
outlives its fill ticket. The epoch fences publication after unsafe
writes or generation retirement. Cache hits copy headers and share the
body. Evaluating a sharded index or a different eviction policy
requires representative multi-key workloads.
[Moka weighted capacity and initialization](https://docs.rs/moka/latest/moka/future/struct.Cache.html).

An upstream capture observes the validated body before downstream
compression. Vary is bounded by declared selectors, and credentialed
traffic and private responses stay outside the shared cache. Compression
uses typed streaming codecs and per-route budgets. Static precompressed
variants follow the same filesystem confinement rules.
[RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html)
[Pingora cache pipeline](https://github.com/cloudflare/pingora/blob/main/pingora-proxy/src/proxy_cache.rs)
[Varnish grace and keep](https://varnish-cache.org/docs/trunk/users-guide/vcl-grace.html).
