# Memory HTTP cache

Enable the shared memory cache by adding `cache` to a proxy route.
The origin must mark a response `public` or include `s-maxage`.
`max_size`, `max_entries` and `max_entry_size` bound the retained
index and representations. `max_fills` and `max_waiters` bound concurrent
publication and collapse. The representation is shared by reference on
a hit. Cache policy and storage are separate modules.
[Cache example](../examples/content-and-cache.yaml).

The key includes the effective downstream scheme, normalized authority,
effective path and query and configured header values. Header values
have explicit length and field boundaries. The route and configuration
generation supply additional isolation. The cache can accept only Vary
fields listed in `vary`. A response with Vary: *, an unconfigured Vary
field, a Set-Cookie field or private/no-store policy stays forward-only.
Requests with authorization, cookies, range, request bodies, a
configured bypass header or no-store bypass storage and reuse.
Apply this policy to public routes and configure every representation
selector used by the origin.

GET can fill the cache. HEAD and client conditionals can reuse a fresh
response. `ttl` caps origin freshness and supplies the fallback for
explicitly public responses. Freshness uses s-maxage, max-age, Expires,
Date and corrected Age. A cache miss with only-if-cached returns 504.
Expiration or explicit no-cache can initiate validator-based revalidation.
A validated 304 refreshes permitted metadata and keeps the stored body.
A privately marked or incompatible 304 invalidates the entry.

`stale_if_error` permits a previously public response after a transport
or HTTP server failure. `stale_while_revalidate` can serve stale to
followers while the first request waits for revalidation. The first
request triggers that revalidation synchronously. Origin directives and
client freshness requirements can disable either stale path.
`invalidate_on_write` defaults to true and clears variants for the
effective URI after a successful unsafe request. Successful reload
invalidates generation-local cache state, including outstanding fills.

The cache streams the first response while capturing at most the
configured entry budget. Truncation, cancellation, trailers, an
oversized response or an invalidation fence discards the candidate.
The lead request's cancellation allows a waiter to become the next
publisher. Each follower retains its own request deadline and identity.
Cache-Status and Prometheus counters expose hits, misses and bypasses
without changing the collector schema. [Policy and storage decision](adr/0013-content-and-cache.md).

This is a conservative shared-cache profile under RFC 9111. Range
combination, authenticated caching, general cache extensions, disk
storage, cluster invalidation and background revalidation remain
outside the profile. NGINX's proxy_cache and Varnish have different
storage and eviction semantics. [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html).
