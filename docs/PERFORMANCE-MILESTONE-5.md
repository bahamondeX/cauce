# Milestone 5 · Production reverse proxy

The checkpoint adds trusted client identity, bounded route actions, independent
weighted and hash-balanced upstream groups, and reload-stable traffic admission.
It uses the existing transport and JSONL contract. Compact pooling remains the
default. [Configuration example](../examples/production-gateway.yaml)
[Policy ordering](adr/0011-compiled-gateway-policies.md).

## Qualified performance and open gates

No new competitive release-performance result was obtained. The AMD64 result
for `47da8fe` remains scoped to a one-worker, cleartext HTTP/1.1 comparison
with logging disabled at 1,000 offers/s. Its 13% CPU advantage applies to
that result. Its 7,500/s confirmation is a lower bound. Current AMD64 and
ARM64 capacity, CPU per success, tail latency, memory, scaling and compact
versus sharded performance need qualified runs.
[Native performance history](PERFORMANCE-NATIVE.md)
[Qualification method](BENCHMARK-METHODOLOGY.md).

### AMD64 8,000/s warm-up diagnosis

The original one-worker Cauce archive contains 237 warm-up dispatch queue
rejections between 342.018 and 371.518 ms. At the first rejection there
were 32 started requests without a completion and 64 admitted offers
without a recorded start. No completion occurred during the rejection
interval. The adjacent recorded completions were separated by 41.663 ms.
The direct 16,000/s preflight completed all 32,000 warm-up offers with zero
queue rejections. Two direct 32,000/s controls also filled the 32/64
dispatch pipeline and rejected 1,246 and 1,960 warm-up offers.

The recorded loss happened at the generator dispatch queue. The cause of the
preceding completion stall remains open. The available archive has no
connection-stage timestamps or scheduler traces to separate scheduling,
connection activity, resource admission and response timing. The 8,000/s
point remains unqualified. The next native rerun should record per-stage
timing on a separate diagnostic pass and repeat the existing independent
generator and backend controls.
[Derived timeline](../bench/program/evidence/milestone-5/warmup-phases.json)
[Reconstruction script](../bench/program/evidence/milestone-5/inspect-warmup.py).

### Feature overhead observations

The isolated request-permit allocation probe measured one 32-byte
allocation through the baseline path and one 32-byte allocation through
the disabled-policy path. The probe counts the current thread around this
single acquisition. The cleartext gateway diagnostic below measures the
whole process.

The unconfigured identity, route action, ACL and rate-zone paths take
branches against compiled optional fields. The one-endpoint fast path
bypasses balancing-group state. Enabled rewrite captures allocate the
rendered destination. An enabled keyed rate zone holds a bounded entry
table and synchronizes accounting. Burst delay consumes an existing
request slot until the delay completes or its deadline expires. New keys
at a full table fail closed when there is no fully repaid, inactive entry.
Real-socket tests verify release on stream completion, socket closure and
reload. They establish functional recovery within the exercised limits.

## Regression evidence

| Gate | Outcome |
| --- | --- |
| Formatting, strict Clippy and all-target Rust | Passed. 63 tests passed and one optional microbenchmark was ignored. |
| Existing proxy integration | 24 cases each for compact and sharded pooling. |
| Pinned NGINX OSS 1.30.8 gateway sockets on Linux ARM64 | 12 records per strategy, comprising nine shared records and three Cauce-specific security and lifecycle records. |
| YAML, TOML and migration | Five YAML examples passed JSON Schema validation, the production example passed the binary configuration check, and five migration tests passed. Shared YAML/TOML validation and atomic reload are covered in the integration suite. |
| Collector | 23 functional cases and 13 storage cases passed. |
| Linux resilience and protocol lifecycle | Proxy failures, log-disk recovery and collector storage suites completed. HTTP/2 and gRPC lifecycle and stress passed, as did nine WebSocket cases and eleven TLS lifecycle cases. |
| AddressSanitizer | The pinned Linux HTTP/2 header observer completed 100,000 executions. |

[Compact socket results](../bench/program/evidence/milestone-5/gateway-compact.jsonl)
[Sharded socket results](../bench/program/evidence/milestone-5/gateway-sharded.jsonl)
[Regression archive](../bench/program/evidence/milestone-5/regression.tar.gz)
[Sanitizer log](../bench/program/evidence/milestone-5/asan-h2-headers.log.gz)
[Evidence checksums](../bench/program/evidence/milestone-5/SHA256SUMS).

## AWS recovery

The AWS connector requires reauthentication. STS through the local SDK
returned `LoginRefreshRequired`. The pending interactive command is
`aws login --profile default`. Use the browser session for the authorized
account ending in `6271`, then verify STS identity before any resource
operation. This checkpoint created or modified zero AWS resources.

EC2 termination for the prior AMD64 instance and removal of campaign-owned
EBS, security group and IAM resources remain unverified. Guest poweroff
is recorded in the original lifecycle. The historical $2.14 and $2.47
figures are list-rate scenarios. Actual charges and usable campaign
budget remain unverified. Resolve those items before a new launch.
The updated ARM64 bundle is staged locally and its checksums were
validated. Its runtime binary corresponds to `2a2ea8b`.
[AWS recovery evidence](../bench/program/evidence/milestone-5/aws-recovery.json)
[Original lifecycle](../bench/program/evidence/milestone-3/aws-lifecycle.json)
[ARM64 bundle identity](../bench/program/evidence/milestone-5/arm64-stage.json).

## Cleartext gateway overhead diagnostic

The Linux ARM64 Colima guest ran one worker and 100 HTTP/1.1 offers/s
with JSONL disabled. The three cells used the previous runtime
(`d20c509`), the current runtime with optional gateway policies disabled,
and the current runtime with a trusted peer, CIDR allowlist, capture
rewrite, shared client rate and concurrency zone, and a per-peer socket
cap. The trusted peer supplied no forwarding header. The test used one
upstream and did not exercise TLS or active probes. The proxy, backend,
generator and process sampler used separate CPUs. Each cell completed
1,100 measured offers and 200 warm-up offers in both runs.

| Cell | Allocations per success | Allocated bytes | Peak heap | Proxy CPU sample | RSS / PSS at sample end | p99 / p99.9 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Previous runtime | 27.226 | 23,381,672 | 539.57K | 19 ticks | 5,376 / 3,934 KiB | 7.96 / 45.08 ms |
| Current, disabled | 27.029 | 22,366,452 | 451.15K | 18 ticks | 5,476 / 4,031 KiB | 7.68 / 22.35 ms |
| Current, enabled | 32.197 | 22,548,705 | 413.08K | 21 ticks | 6,096 / 4,662 KiB | 8.23 / 31.94 ms |

Heaptrack totals include startup, warm-up and shutdown. CPU, RSS, PSS
and latency come from a separate run without Heaptrack. The CPU snapshot
difference covers approximately eight seconds at 100 ticks/s. The guest
cgroup was shared, so its memory cannot be attributed to the proxy.
The enabled fixture recorded 5.168 additional allocations per success
and 620 KiB higher proxy RSS at sample end than the disabled fixture. The
individual cells have one repetition and do not establish a release
throughput, CPU or latency effect.
[Diagnostic fixture](../bench/program/fixtures/gateway-enabled.toml)
[Summary](../bench/program/evidence/milestone-5/gateway-overhead.json)
[Raw offers, Heaptrack and process snapshots](../bench/program/evidence/milestone-5/gateway-overhead-raw.tar.gz).

## Independent health and TLS increments

HTTP/2 health probes check 2xx headers. Standard unary gRPC health probes
require SERVING and successful trailers, and enforce a 1 KiB message cap
and an absolute probe timeout. The real-socket test with the standard
grpcio health service exercises ejection, recovery, accepted reload and
rejected reload. Active probes use separate sockets and are disabled by
default. Exact SNI selection and a default certificate, mutual TLS in
both directions, configurable TLS versions and resumption controls use
rustls. The TLS socket test exercises failure with missing and untrusted
client certificates, peer-name verification, session resumption,
certificate rotation, generation isolation and rollback.
[Health configuration](../examples/http2-health.yaml)
[Mutual TLS configuration](../examples/gateway-mtls.yaml)
[TLS and reload decision](adr/0012-gateway-tls-identity.md).

### Combined runtime validation

The combined runtime `2a2ea8b` passed formatting and strict Clippy,
64 Rust tests, 24 proxy integration cases per pool strategy, and 12
pinned NGINX socket records per pool strategy. Seven YAML examples validated
against the schema and six migration tests passed. The combined Linux
ARM64 release passed the HTTP/2 and gRPC lifecycle, absolute header
deadlines, HTTP/2 stress, resilience, TLS and WebSocket gates. It also
passed both collector suites. The header-observer fuzz inputs retained
their original hashes. Its AddressSanitizer evidence is above.
[Combined raw regression archive](../bench/program/evidence/milestone-5/combined-regression.tar.gz)
[Combined compact sockets](../bench/program/evidence/milestone-5/gateway-final-compact.jsonl)
[Combined sharded sockets](../bench/program/evidence/milestone-5/gateway-final-sharded.jsonl).
