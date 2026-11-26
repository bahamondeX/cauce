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
| Unmapped directive registrations | 620 | 617 |
| Verified equivalent modules or registrations | 0 | 0 |
| Mapped configure patterns | 60 | 60 |

The compatibility CLI produces audit, queue and report outputs. The queue
has a stable identifier and exact registration provenance for each entry.
Parser arity comes from the pinned source. Names and values accepted by
setters, defaults and inheritance require semantic review. The report
leaves unreviewed fields pending and keeps build flags outside runtime
equivalence counts. [Compatibility tooling](COMPATIBILITY-TOOLING.md).

## Verified slices

- The pinned NGINX loopback run passed 23 request cases and seven
  configuration rejection checks across HTTP core, proxy and the response
  header filter. Two configuration rejections compared both engines. Five
  exercised Cauce's explicit validation guards.
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

[Pinned differential cases](../bench/program/evidence/milestone-10/differential-final.json)
[SQLite security evidence](SQLITE-SECURITY.md)
[Read-only lockfile investigation](LOCKFILE-RECOVERY.md).

## Performance scope

The compatibility harness records process RSS, cumulative CPU time and
shutdown behavior around loopback fixtures. These observations describe the
test processes. The original Milestone 9 binary-size result remains scoped
to its matched source and toolchain. Native throughput, CPU per success,
tail latency and multicore qualification require matched runs on native
Linux equipment. No AWS infrastructure was provisioned.

[Native preparation](PERFORMANCE-NATIVE.md)
[Remaining compatibility and release gates](CAUCE-1.0-GAP-ANALYSIS.md).
