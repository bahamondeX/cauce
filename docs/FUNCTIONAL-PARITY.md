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
