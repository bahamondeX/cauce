# Milestone 10 compatibility evidence

The implementation starts at `c1c6231`. The NGINX OSS reference stays
at 1.30.8. The compatibility registry counts 120 modules, 754 directive
registrations and 149 configure patterns throughout this milestone. njs
and nginx-acme have independent counts.

| OSS coverage | Before | After |
| --- | ---: | ---: |
| Partially implemented modules | 40 | 40 |
| Unmapped modules | 80 | 80 |
| Partially implemented directive registrations | 134 | 137 |
| Mapped directive registrations awaiting implementation | 0 | 2 |
| Unmapped directive registrations | 620 | 615 |
| Verified equivalent modules or registrations | 0 | 0 |
| Mapped configure patterns | 60 | 60 |

The compatibility CLI produces audit, queue and report outputs. The queue
has a stable identifier and exact registration provenance for each entry.
Parser arity comes from the pinned source. Names and values accepted by
setters, defaults and inheritance require semantic review. The report
leaves unreviewed fields pending and keeps build flags outside runtime
equivalence counts. [Compatibility tooling](COMPATIBILITY-TOOLING.md).

The nginx-acme 0.4.2 source review covers all 15 registrations. Ten have
partial Cauce behavior and five have mapped gaps. The entries preserve
issuer-block contexts and keep the Cauce-only Pebble evidence separate
from add-on differential evidence. njs retains four modules and 55
unmapped registrations.

## Verified slices

- The pinned NGINX loopback run passed 27 request cases and eight
  configuration rejection checks across HTTP core, proxy, the response
  header filter and address access. Three configuration rejections compared both engines. Five
  exercised Cauce's explicit validation guards.
- Address access cases matched default permission, CIDR admission, inherited
  denial, child policy replacement and invalid CIDR rejection. A denied
  response's error-page body was outside the compared fields.
- Nested lookup verifies exact matches, the longest inclusive path,
  scoped regex order, the effects of `^~` at different levels, and nested
  policy inheritance. The pinned Darwin build accepted a literal child
  under a regex location but selected the parent in the probe. Cauce
  reports that construct during validation.
- Literal response additions verify ordered repeated fields, empty values,
  inheritance, local overrides and `always` on an error response. HTTPS
  origin checks verify explicit SNI on and off. The negative TLS case
  rejects a mismatched certificate identity when SNI is off.
- Fourteen migration tests passed. Literal previews map source locations
  to Cauce route IDs, materialize verified inheritance, and name the
  minimum distribution. They continue to report manual review.
- The SQLite update reports linked SQLite 3.54.1. Collector integration,
  storage, SIGKILL and real ENOSPC tests passed. The advisory scan returned
  zero vulnerabilities and zero warnings for the recorded advisory snapshot.
- Both pool layouts passed IPv4-to-IPv6 DNS rotation with an active HTTP/2
  response and established Stream TCP and UDP traffic. A verified TLS
  POP3 fixture independently held an authenticated session through the
  same address rotation. Fresh connections selected the new address and
  established work finished on the old address.
- Eight concurrent requests shared one disk fill. A restored 4 MiB
  response finished after its disk file was evicted. Real ENOSPC on a
  dedicated HFS+ test image preserved memory-cache reads and allowed disk
  writes to resume after space returned.
- The local protocol rerun passed independent HTTP/3, gRPC health,
  Stream and Stream TLS, synthetic application and mail, compression, and
  Pebble renewal cases. Five optional cases requiring PHP-FPM, application
  reference servers or Postfix/Dovecot containers were skipped in this
  rerun. The pinned HTTP/3 client receives connection-level GOAWAY apart
  from request data and trailers.

[Pinned differential cases](../bench/program/evidence/milestone-10/differential-final.json)
[SQLite security evidence](SQLITE-SECURITY.md)
[Read-only lockfile investigation](LOCKFILE-RECOVERY.md).

## Performance scope

The tested runtime is `6aa4c261939ffcb04b8b17e101fd5aedac4dae9e`. The
Full and Core Linux ELFs are paired by architecture and use Rust 1.89.0,
Zig 0.15.2 and glibc 2.36. All six profiles also passed host configuration
checks. Functional smoke tests used the existing Linux ARM64 guest and
QEMU user-mode AMD64. All four collectors reported SQLite 3.54.1.

| Architecture | Full gateway bytes | Core gateway bytes | Core ELF reduction | Full OCI layer bytes | Core OCI layer bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Linux ARM64 | 13,902,256 | 10,211,688 | 26.55% | 37,359,812 | 35,680,558 |
| Linux AMD64 | 15,817,184 | 11,745,024 | 25.75% | 38,010,426 | 36,278,246 |

OCI values sum compressed layers for one platform, including the Debian
base and both executables. The layouts passed index, manifest, config,
layer, and embedded binary digest verification. Linux smoke tests ran the
staged executables directly. The OCI layouts have package-integrity evidence
and await container runtime checks.

A single-client HTTP/1 diagnostic in the Linux ARM64 guest made three
rounds of 2,048 validated requests to each profile/configuration after
64 warm-up requests per run. The payload was a repeating 4 KiB body.
The ARM64 figures describe this loopback fixture and virtual machine.

| Case | Wire body bytes | Startup RSS / PSS / private dirty, median KiB | Range of p50 across runs, µs | Range of p99 across runs, µs |
| --- | ---: | ---: | ---: | ---: |
| Full, gzip disabled | 4,096 | 7,868 / 6,493 / 876 | 45–78 | 84–100 |
| Full, gzip enabled | 45 | 7,872 / 6,497 / 880 | 88–126 | 149–152 |
| Core, compression excluded | 4,096 | 7,000 / 5,625 / 756 | 46–83 | 92–97 |

The gzip-disabled Full and Core timing ranges overlap. Readiness samples
use 25 ms polling. Raw startup, CPU and shutdown samples are preserved.
The Darwin fixture verified the same responses with nested routing and
literal header additions toggled. Across three rounds of 1,024 requests
per case its individual p50 values ranged from 44–60 μs with the features
disabled, 44–60 μs with nested routing and 61–62 μs with response additions.
The diagnostics leave the allocation and physical-footprint comparisons
open. Native CPU per successful request, workload tail latency,
sustainable goodput and multicore qualification still require matched
Linux hardware. No AWS infrastructure was provisioned.

[Artifact identities](../bench/program/evidence/milestone-10/artifact-identities.json)
[OCI manifests](../bench/program/evidence/milestone-10/oci-identities.json)
[ARM64 diagnostic samples](../bench/program/evidence/milestone-10/overhead-linux-arm64.jsonl)
[Darwin diagnostic samples](../bench/program/evidence/milestone-10/overhead-darwin-final.jsonl)
[Evidence SHA-256 manifest](../bench/program/evidence/milestone-10/SHA256SUMS).

[Native preparation](PERFORMANCE-NATIVE.md)
[Remaining compatibility and release gates](CAUCE-1.0-GAP-ANALYSIS.md).
