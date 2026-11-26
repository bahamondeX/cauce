# Milestone 9 functional and release evidence

The work starts at `35616a021fb889f6fc12510def8ade68a525117f`.
Compact pooling remains the default. The sharded alternative passed the
DNS rotation fixture and HTTP regression suite.

| Slice | Local evidence |
| --- | --- |
| Shared DNS | Loopback IPv4 and IPv6, CNAME, TTL rotation, NXDOMAIN, SERVFAIL, cancellation and concurrent refresh. Real HTTP/1 pooled requests and established TCP sessions exercised both pool strategies. |
| Resilience | Circuit state tests and real sockets for half-open admission, bounded pre-send failover and non-idempotent handling. |
| Cache | 11 shared memory-cache socket cases, Loom publication fencing, and three disk-tier cases covering reads after memory eviction, corruption, quotas, mutation invalidation and recovery. |
| TLS | NGINX OSS 1.30.8 Stream differential for named/default certificates and bidirectional verification. Pebble v2.9.0 verified HTTP-01 issuance, renewal with an open HTTPS connection, trusted QUIC handshake and secure persisted restart. |
| Migration | 11 tests. Include root bounds, provenance, inherited literal policy, variable analysis and validation before candidate publication. |
| Supply chain | CycloneDX, dependency notices and two RustSec snapshots. The final snapshot flags bundled SQLite and no warnings. |

The first DNS checkpoint's full Rust run hit a collector rename-gap
failure. The isolated retry and subsequent combined run passed. The
collector code and external contract were preserved. The raw evidence
also retains the cancellation-fixture connection resets.

No new AWS resources were provisioned. Current native competitive
goodput, tails, CPU, allocation and multicore results are open. The
historical AMD64 CPU comparison stays scoped to its measured reference.
Optional build-size and local process diagnostics apply only to the
manifest and workload listed with their evidence.

[Raw evidence](../bench/program/evidence/milestone-9/)
[1.0 gap analysis](CAUCE-1.0-GAP-ANALYSIS.md)
[Native evidence](PERFORMANCE-NATIVE.md).
