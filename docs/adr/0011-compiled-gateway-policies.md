# ADR 0011 · Compiled gateway policies

The HTTP engine uses concrete policy fields on a validated configuration
generation. Its request stages run in this order.

1. Check protocol framing, authority, and path before policy evaluation.
2. Resolve trusted client identity from the transport peer and the configured header.
3. Select the virtual host and location.
4. Apply the location action. Internal jumps reselect or enter a named location.
   An external redirect ends the request in this stage.
5. Apply the final route's client allowlist.
6. Apply the final route's admission zone.
7. Select an eligible endpoint.
8. Canonicalize forwarding identity and apply compiled request header edits.
9. Execute the upstream request through the existing pool and transport.
10. Validate the response and apply allowed response edits.
11. Complete the existing JSONL event and release request permits.

Global socket and request admission protects normalization itself. Names
remain scoped to the authority declaration that selected the route.
Rewrite and named actions preserve the request method and body.
Ten internal hops bound cycles. Runtime destinations pass the same raw
path safety checks as incoming requests. Rewrites use the bounded Rust
byte-regex engine and numbered captures. PCRE-only expressions and nested
NGINX location semantics remain outside the shared profile.

Real-IP resolution accepts a bounded sequence only when the immediate
peer is trusted. Replacement sends the resolved client address. Chain
preservation emits that address and its verified suffix. The original
socket peer remains available to connection admission and opt-in
diagnostics. The collector schema retains its established fields.

Admission zones belong to the running process. Their specifications
and the TCP peer cap require restart. Routes can be reloaded against
existing zones. A full table retains active entries and entries with
pending rate debt. It rejects a new key when no entry can be safely
removed. Concurrent request permits share the upload and response
lifetime, including HTTP/2 streams and upgraded tunnels. Delay occupies
the existing global request budget and remains bounded by the absolute
request deadline.

Each route declares its upstream group. Multiple routes get independent
selection cursors even if they refer to the same endpoints. A single
upstream bypasses group state. Consistent hashing uses weighted rendezvous
selection over endpoint IDs. The NGINX, Pingora, and HAProxy hash
implementations use different structures and hash mappings.
[Pinned NGINX source](https://nginx.org/download/nginx-1.30.8.tar.gz)
[Pinned Pingora selector](https://github.com/cloudflare/pingora/blob/f86ffda5d3ae5c3baf5125682138b33b26c01b08/pingora-load-balancing/src/selection/mod.rs)
[Pinned HAProxy source](https://www.haproxy.org/download/3.4/src/haproxy-3.4.8.tar.gz).
