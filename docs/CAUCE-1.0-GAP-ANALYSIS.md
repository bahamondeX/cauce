# Cauce 1.0 gap analysis

The fixed OSS denominator is NGINX 1.30.8. The source, checksum,
module variants and parser command contexts live in the pinned registry.
The JSONL backlog emits one row per module, directive registration and
configure option pattern. Shared fixtures verify the scope described
by the fixture, while the associated directive and module stay partial
until their full supported semantics have evidence.

| Reference | Entries | Partial or mapped | Unmapped | Equivalent |
| --- | ---: | ---: | ---: | ---: |
| OSS modules | 120 | 40 partial | 80 | 0 |
| OSS directive registrations | 754 | 137 partial, 2 mapped but unimplemented | 614 | 1 |
| OSS configure patterns | 149 | 60 mapped | 89 | Build scope |
| njs modules | 4 | 0 | 4 | 0 |
| njs directives | 55 | 0 | 55 | 0 |
| nginx-acme modules | 1 | 1 partial | 0 | 0 |
| nginx-acme directives | 15 | 10 partial, 5 mapped but unimplemented | 0 | 0 |

Unmapped entries have no established mapping in the current audit.
They remain implementation or semantic review work. Configure options
are tracked as build, packaging, dependency, platform or deployment
choices. Official add-ons are separate.

[Source registry](../compatibility/nginx-oss.yaml)
[Machine-readable backlog](../compatibility/nginx-gap-backlog.jsonl)
[Generator](../scripts/nginx-gap-audit.py).

The verified registration is HTTP core `internal`. Request variables
and location inheritance support its proof. The semantic graph
identifies the larger configuration, expression and execution-phase
dependencies before expanding the exact-equivalence batch.
[Semantic architecture](SEMANTIC-ARCHITECTURE.md).

## Release gates

| Priority | Remaining work | Verified base |
| --- | --- | --- |
| P0 | Extend resilience and active health policy to Stream and mail. Exercise additional DNS outage, reconnection and high-concurrency cases. | Shared recursive DNS with address rotation in HTTP/1, HTTP/2, TCP, UDP and authenticated POP3 fixtures. HTTP and application circuit breaking and one budgeted pre-transmission retry. |
| P0 | Qualify long-run disk I/O stability and native disk-cache performance. Evaluate preserving verified entries after an unclean shutdown. | Bounded disk tier, corruption checks, write invalidation, verified clean reconstruction, cold crash recovery and Linux tmpfs ENOSPC recovery. |
| P0 | Qualify public ACME interoperability and sustained traffic during renewal. Add a DNS-01 provider contract. | Local Pebble HTTP-01 issuance, overlapping certificate precedence, renewal, last-known certificate service after a failed renewal, persistence, HTTPS connection survival and a verified QUIC handshake. |
| P0 | Implement ordered address rules, method-specific access policy and composition of authentication and address checks. | Route IPv4/IPv6 allowlists, with matching IPv4 access and inheritance decisions in five pinned cases. |
| P1 | Complete retained-socket configuration and certificate changes for Stream and mail. | HTTP and QUIC reload, Stream and mail binding additions/removals, bind rollback and protocol-aware draining. |
| P1 | Complete inheritance across contexts, PCRE-only behavior, maps, variables, buffering, temporary files, subrequests, conditional filters and remaining web modules. | Scoped nested location precedence, literal response additions, upstream SNI control, named jumps and selected compiled policies. |
| P1 | Expand migration to application, caching and mail deployments with complete unsupported behavior accounting. | Bounded include expansion, literal nested routing, source mappings, variable diagnostics, selected policy inheritance and an optional generated-YAML check. |
| P1 | Qualify disabled-module cost and native capacity for each distribution. | Cargo composition, explicit capability rejection and build staging. |
| Independent | Evaluate sandboxed JavaScript for njs semantics, and a separate WASM extension interface with explicit capabilities and resource budgets. | Separate njs accounting. |
| Release | Qualify native Linux ARM64, current AMD64 and multicore performance, including all benchmark families. | Historical baseline and local functional evidence. |

[Native performance](PERFORMANCE-NATIVE.md)
[Remediated SQLite advisory](SQLITE-SECURITY.md)
[Milestone 10 evidence](PERFORMANCE-MILESTONE-10.md)
[Milestone 9 evidence](PERFORMANCE-MILESTONE-9.md).
