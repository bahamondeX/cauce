# Milestone 9 functional and release evidence

The work starts at `35616a021fb889f6fc12510def8ade68a525117f`.
Compact pooling remains the default. The sharded alternative passed the
DNS rotation fixture and HTTP regression suite.

| Slice | Local evidence |
| --- | --- |
| Shared DNS | Loopback IPv4 and IPv6, CNAME, TTL rotation, NXDOMAIN, SERVFAIL, cancellation and concurrent refresh. Real HTTP/1 pooled requests and established TCP sessions exercised both pool strategies. |
| Resilience | Circuit state tests and real sockets for half-open admission, bounded pre-send failover and non-idempotent handling. |
| Cache | 11 shared memory-cache socket cases, Loom publication fencing, and three disk-tier cases covering reads after memory eviction, corruption, quotas, mutation invalidation and recovery. |
| TLS | NGINX OSS 1.30.8 Stream differential for named/default certificates and bidirectional verification. Pebble v2.9.0 verified HTTP-01 issuance, overlapping certificate precedence, renewal with an open HTTPS connection, trusted QUIC handshake and secure persisted restart. |
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

## Regression and qualification notes

- Compact and sharded HTTP integration each completed 24 cases including
  smuggling, admission, schema checks and reload. The Stream fixture
  completed 11 cases, HTTP/3 completed nine, FastCGI completed eight,
  and the WSGI gateway suite completed four. The real client fixtures
  cover PHP-FPM, both uWSGI modes, Postfix, Dovecot and NGINX 1.30.8.
- Rust ran 60 gateway tests and 14 collector tests. One local diagnostic
  is ignored by default. Formatting, Clippy and the cache publication Loom
  model passed.
- The application wire codec completed 100,000 AddressSanitizer fuzz runs
  in the Linux ARM64 guest on the runtime tree behind `cbb1a79`. The seed
  was 381109 and the maximum input length was 4 KiB.
- Stream and mail topology tests verified additions, failed-bind
  rollback, removals and retirement of established sessions. The
  retained-socket settings and certificate changes listed in the
  listener guide still require restart.
- Linux ARM64 disk testing used a 128 KiB tmpfs and confirmed actual
  ENOSPC. The failing write left no published object, the memory hit
  remained available, and persistence recovered after capacity returned.
- The first collector storage rerun started its rotation stress before
  the process owned its state. The fixture now waits for the state
  lock. The rerun passed all 13 storage cases. The collector runtime
  and contract were untouched.
- The Darwin NGINX routing fixture stops after 14 matches on the
  uppercase PNG regex case. Milestone 8 records the same result.
  The pinned Alpine NGINX in the ARM64 guest passed all 20 routing cases,
  11 gateway cases with each pool layout and 15 static and compression
  cases. Initial Linux fixture setup failures are retained alongside the
  passing comparisons. Equality is scoped to each fixture's assertions.
- The RustSec database is pinned in the fixture manifest. The final
  scan still reports RUSTSEC-2026-0459 for bundled SQLite. The source
  and license checks passed under cargo-deny for both Linux targets,
  with dependency and license warnings retained in the report.

## Local disk benchmark diagnostic

The existing open-loop content runner can select the disk cache and
start with an empty memory cache after clean shutdown. The Darwin
loopback diagnostic sent 30 requests at 15 requests/s for a 1 KiB object;
all 30 succeeded and the origin received one request for priming. This
fixture exercises the restart path. Native disk I/O, eviction throughput,
allocation cost and container size remain unmeasured for this candidate.
[Raw arrivals and audit](../bench/program/evidence/milestone-9/disk-restart-smoke/).

## Source and binary identity

The Linux disk and NGINX fixtures used Full binaries from
`36b73b1949d5179167624fd5d876f4df36c8e432`, built in the local ARM64
guest. The proxy is 13,717,976 bytes and the collector is 5,860,480 bytes.
The corresponding cross-built Linux AMD64 proxy is 15,766,136 bytes
and the collector is 6,609,520 bytes. The AMD64 fixture verified the ELF
architecture and hashes, then ran capability discovery and configuration
validation under emulation.

The matched `36b73b1` staging matrix contains all four Full and Core
builds. Core is 10,044,120 bytes on ARM64 and 11,666,288 bytes on AMD64,
26.8% and 26.0% smaller than Full on the same architecture. Both Core
binaries accepted the base configuration and rejected an HTTP disk cache
configuration with an explicit feature error. The AMD64 smokes ran under
emulation. The initial batch script stopped after export because its
source changed during execution. Independent per-target staging produced
the same Core binary hashes and verified the completed exports.

The separate Linux ARM64 Core build from `cbb1a79` is 10,109,656 bytes.
It passed capability discovery and an explicit configuration rejection
for the disk-cache example. The source boundary differs from the Linux
Full snapshot. The matched Linux and Darwin comparisons hold source fixed.
The main Darwin socket regression used a debug snapshot from that
checkpoint. Later disk invalidation, listener retirement and mail DNS
changes have separate focused evidence. The staging and evidence
manifests record the source tree, lockfile hash and binary hashes.
[Linux ARM64 identities](../bench/program/evidence/milestone-9/linux-arm64-release.txt)
[Linux AMD64 identities and smoke check](../bench/program/evidence/milestone-9/qualification/linux-amd64/)
[Linux ARM64 Core identities and smoke check](../bench/program/evidence/milestone-9/qualification/linux-arm64-core/)
[Matched Linux staging matrix](../bench/program/evidence/milestone-9/qualification/linux-matrix/)
[Evidence checksums](../bench/program/evidence/milestone-9/SHA256SUMS).

## Distribution checks

All six profiles independently compiled and passed CLI capability and
configuration checks on Darwin ARM64. The Full debug binary's crate
breakdown puts Quinn, ACME, compression and Hickory among the larger
dependency contributions. The independent `cargo llvm-lines` debug
analysis highlights TOML deserialization and the gateway request future.
Those tools attribute debug code and monomorphizations. The following
measurements use stripped release builds from the same `cbb1a79` tree.

| Darwin ARM64 profile | Binary bytes | Median to ready | Median idle RSS | Median peak physical footprint |
| --- | ---: | ---: | ---: | ---: |
| Full, compression disabled | 13,575,712 | 12.47 ms | 6,224 KiB | 2,392,768 bytes |
| Full, compression configured | 13,575,712 | 12.88 ms | 6,288 KiB | 2,417,344 bytes |
| Core | 10,045,776 | 8.93 ms | 4,632 KiB | 1,729,216 bytes |

Each row uses ten starts with one worker, a successful admin readiness
request, 100 ms of idle time, and graceful shutdown. The optional
compression setting applies to a route that receives no request in this
fixture. Core is 26.0% smaller on disk and its `__TEXT` segment is
3,342,336 bytes smaller. Startup and memory measurements are local
diagnostics and include ambient host activity. Native throughput, tail
latency, allocations per request and load with compression still require
matched hardware and workloads.

The ARM64 Linux lab image reports 135,155,426 bytes of uncompressed
image content. It overlays the `36b73b1` binaries on the Milestone 8
runtime image for regression. After one verified HTTP/1.1 direct
response, `/proc/1/smaps_rollup` reported 8,348 KiB RSS, 7,495 KiB PSS
and 896 KiB private dirty pages. These figures describe one observation
inside the VM and are separate from the Darwin measurements.

The same Darwin Full binary passed both pool layouts and the focused
DNS, resilience, cache and listener rollout fixtures. In the Linux ARM64
guest, the Full artifact from `36b73b1` passed the HTTP/2 and gRPC suite,
20 header deadline cases, cleartext and TLS stress, and the proxy, log
disk and collector storage resilience suite. Eleven TLS lifecycle cases,
nine WebSocket cases and the collector contract against a synthetic HTTPS
sink also passed. Setup retries corrected the Compose plugin path, fixture
certificate setup and Docker fixture startup timing. The rejected runs
remain in the evidence. The HTTPS sink exercises the collector wire
contract locally.
[Profile checks and debug attribution](../bench/program/evidence/milestone-9/qualification/).
