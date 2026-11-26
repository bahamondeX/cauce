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
