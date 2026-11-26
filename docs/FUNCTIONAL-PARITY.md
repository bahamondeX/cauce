# NGINX OSS functional parity

[Cauce's compatibility registry](../compatibility/nginx-oss.yaml) records
the pinned NGINX release, module variants, directive registrations,
configure options and observable behaviors. Official add-ons are
listed separately. A directive stays partial until its documented
semantics are covered.

## Milestone 3 routing slice

Cauce selects a virtual host by exact authority, longest leading wildcard
and longest trailing wildcard. Within that virtual host, a literal
exact location takes priority over the longest segment-aware prefix.
A missing location returns 404 for the selected host. Reload compiles
and validates the complete candidate before publishing it.
[Configuration and YAML example](CONFIGURATION.md#virtual-hosts-and-locations)
[Routing decision](adr/0009-virtual-host-and-location-precedence.md).

The Linux ARM64 real-socket comparison with NGINX OSS 1.30.8 matched all
11 shared cases. They include uppercase hostnames, multiple wildcard labels,
overlapping wildcard classes, exact-host priority, exact path and query
handling, and an explicit 404 default on the NGINX fixture. Cauce's
authority port remains significant. URI normalization, implicit defaults,
regular expressions, modifiers other than `=`, named locations and
internal redirects still have distinct or missing semantics.
[Differential fixture](../tests/compare_nginx_routing.py)
[Observed responses](../bench/program/evidence/milestone-3/routing-nginx.jsonl).

The offline migration preview translates the covered common names and
literal locations of one IPv4 HTTP server. It includes a confidence
classification and a validation command. Unsupported structures cause
the preview to fail before writing a candidate. YAML and TOML both support
binary byte units, parsed once during configuration preparation. Validation
and atomic reload operate on normalized byte counts.
[Migration usage](CONFIGURATION.md#nginx-migration-report).

## Remaining priority order

| Area | Next verified slices |
| --- | --- |
| Reverse proxy | Default hosts and listener boundaries, redirects and rewrites, header policies, trusted client addresses, buffering, balancing and dynamic upstream lifecycle. |
| TLS | SNI, mutual TLS, certificate policy and renewal with staged rotation. |
| Web serving | Safe static roots, request conditions and ranges, caching and compression. |
| Network gateway | TCP and UDP admission, passthrough, PROXY protocol and connection draining. |
| Additional protocols | HTTP/3, FastCGI, uWSGI, SCGI, mail and remaining modules. |

[Adoption backlog](NGINX-COMPATIBILITY-BACKLOG.md).

## Milestone 4 gateway slice

The flat location table now supports exact, literal prefix, `^~`, `~`
and `~*` precedence on validated raw paths. Regex declarations retain
their configuration order. The migration preview translates flat
literal locations and reports regex and named locations for manual
review. [Advanced YAML example](../examples/advanced-locations.yaml).

Route-local header policies set or remove request and response fields
after hop filtering. Cauce protects forwarding, framing, tracing and
protocol-owned headers. The migration preview translates fixed
`proxy_set_header` values and `proxy_hide_header` on flat locations.
The pinned NGINX differential passed 19 routing cases and one
request/response header policy case over real Linux sockets.
[Header policy YAML](../examples/header-policies.yaml)
[Differential evidence](../bench/program/evidence/milestone-4/nginx-differential.jsonl).

| Registry scope | Verified | Implemented | Partial | Unsupported or unmapped |
| --- | ---: | ---: | ---: | ---: |
| NGINX OSS module definitions | 0 | 0 | 8 | 112 |
| Directive registrations across compiled and conditional variants | 0 | 0 | 26 | 728 |
| Recorded observable behaviors | 6 | 0 | 8 | 19 |
| Configure option patterns | 0 | 0 | 0 | 149 |
| Official add-on directive registrations | 0 | 0 | 0 | 70 |

The official add-on inventory is njs 1.0.2 with 55 registrations and
nginx-acme 0.4.2 with 15. The registry counts registrations independently
of directive names. Its six verified behaviors do not confer full
coverage on any module or directive.
[Machine-readable coverage counts](../bench/program/evidence/milestone-4/coverage-counts.json).

## Milestone 5 production gateway

NGINX OSS 1.30.8 and Cauce exchanged real requests for the shared trusted
client, route action, access, admission and weighted balancing fixtures.
The same suite exercised both compact and sharded pooling. The Cauce-only
security and lifecycle cases cover consistent placement, reload with an
in-flight HTTP/2 stream and physical per-peer socket admission.
[Socket fixture](../tests/compare_nginx_gateway.py)
[Compact results](../bench/program/evidence/milestone-5/gateway-compact.jsonl)
[Sharded results](../bench/program/evidence/milestone-5/gateway-sharded.jsonl).

| Inventory scope | Verified | Implemented | Partial | Unsupported or unmapped |
| --- | ---: | ---: | ---: | ---: |
| NGINX OSS module definitions | 0 | 0 | 13 | 107 |
| Directive registrations | 0 | 0 | 51 | 703 |
| Recorded observable behaviors | 10 | 0 | 12 | 15 |
| Configure option patterns | 0 | 0 | 0 | 149 |
| njs 1.0.2 registrations | 0 | 0 | 0 | 55 |
| nginx-acme 0.4.2 registrations | 0 | 0 | 0 | 15 |

These totals keep full-module and full-directive coverage separate from the
behaviors observed in shared fixtures. Cauce's rewrite syntax supports
bounded Rust regular expressions and numbered captures. NGINX-specific
PCRE constructs, nested location precedence and complex rewrite scripts
still require manual review. Weight proportions were compared in the
socket fixture. IP and consistent-hash placement, connection-limit
semantics and broader `limit_req` behavior remain partial. The
migration preview supports literal server-level real-IP settings and
origin-relative static returns within its existing configuration subset.
[Machine-readable coverage](../bench/program/evidence/milestone-5/compatibility.json)
[Scope and evidence registry](../compatibility/nginx-oss.yaml)
[Production YAML](../examples/production-gateway.yaml).

The gateway also supports exact SNI certificate selection, mandatory
listener-wide mTLS, upstream client certificates, TLS 1.2/1.3 policies
and session resumption controls. The single-server migration preview
maps literal certificate paths, protocol selections, and mandatory
client verification. Active HTTP/2 and standard gRPC readiness probes
are Cauce extensions. The compatibility totals include partial TLS
directive mappings. They do not count active upstream probes as an
NGINX OSS capability. Per-SNI trust, optional client authentication,
DNS-based endpoint retirement, retry budgets and ACME remain open.
[TLS configuration](CONFIGURATION.md#tls-certificates-and-mutual-authentication)
[Active health configuration](CONFIGURATION.md#active-upstream-health).

## Milestone 6 content delivery

The pinned NGINX 1.30.8 socket fixture verifies shared static GET, HEAD,
root, alias, index, literal try_files and direct-response behaviors.
Conditional requests and bounded single, suffix, multipart and date
If-Range responses also match in the tested subset. Gzip comparisons
check decoded content and negotiated headers. Cauce uses weak static
validators and a different directory-listing format. Brotli is available
through Cauce's own configuration.
[Socket fixture](../tests/compare_nginx_content.py)
[Raw outcomes](../bench/program/evidence/milestone-6/nginx-content.jsonl).

Memory proxy caching remains partial relative to proxy_cache. Its
verified local behavior includes public-response caching, bounded
collapse, freshness, revalidation, stale serving and invalidation.
NGINX disk storage and the Cauce memory policy have distinct resource
and lifecycle contracts. [Machine-readable coverage](../bench/program/evidence/milestone-6/compatibility.json).

The pinned inventory records 23 partial modules out of 120 and
71 partial directive registrations out of 754. It has 12 verified
observable behaviors. The 149 configure option patterns remain
inventoried separately. The njs and nginx-acme inventories remain
separate from the core counts.

## Milestone 7 multiprotocol gateway

The pinned inventory contains 120 modules, 754 directive registrations,
149 configure option patterns and 70 separately scoped add-on directive
registrations. The new differential cases cover TCP half-close and
weight distribution, UDP echo boundaries, TLS ClientHello SNI and
default routing, trusted PROXY v1 source and payload propagation, and
an HTTP/3 response over QUIC. [Layer 4 tests](../tests/stream/interop.py)
[HTTP/3 tests](../tests/http3/interop.py).

Cauce also verifies its own IPv6 and Unix transport, ALPN-based preread,
PROXY v2 framing and policies, UDP limits and cancellation. NGINX's
Stream SSL termination, resolver semantics, dynamic upstream areas,
and remaining HTTP/3 behavior need separate implementation and
differential evidence. [Registry](../compatibility/nginx-oss.yaml).

## Milestone 8 application and mail scope

The pinned registry contains 120 OSS modules and 754 directive
registrations. Of these, 39 modules and 116 registrations have partial
mappings. The default unsupported status applies to 81 modules and
638 registrations. The behavioral ledger records 23 verified cases,
22 partial cases and four unsupported cases. It also inventories 149
configure option patterns and five official add-on modules with
70 directive registrations. [Counts](../bench/program/evidence/milestone-8/coverage-counts.json).

The application comparisons cover seven PHP-FPM cases and five each
for native uWSGI and SCGI. The mail comparison covers SMTP submission
with Postfix and IMAP and POP3 login with Dovecot. Script expressions
and inherited gateway configuration require manual migration. Mail
credential rewriting, challenge mechanisms outside the documented
password-based subset, and acceptance of unauthenticated SMTP remain
outside the implemented scope. Shared TTL-aware DNS, advanced resilience,
background cache revalidation, disk cache, ACME and Stream TLS remain
open. [Registry](../compatibility/nginx-oss.yaml) [Interoperability](PERFORMANCE-MILESTONE-8.md).

## Milestone 9 infrastructure slices

Stream TLS shared cases cover default and named certificates, downstream
mTLS, verified upstream TLS and backend hostname rejection against NGINX
OSS 1.30.8. DNS rotation, upstream circuits, detached cache refresh,
disk storage and local ACME have independent Cauce fixtures. These
capabilities also have configuration and lifecycle differences that
remain in the source-derived backlog.

[Gap analysis](CAUCE-1.0-GAP-ANALYSIS.md)
[Milestone 9 evidence](PERFORMANCE-MILESTONE-9.md).

The pinned Alpine Linux ARM64 reference passed 20 routing cases, 11
gateway cases per pool layout and 15 content cases. The Stream TLS
fixture checks SNI selection, mutual authentication and verified backend
hostnames. The registry retains partial status for the corresponding
directive registrations. [Pinned differential evidence](../bench/program/evidence/milestone-9/linux-nginx/).
