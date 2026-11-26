# Milestone 6 · Web server and shared cache

Cauce serves static and direct content through compiled routes. Proxy
routes can enable a bounded shared memory cache. Streaming gzip,
Brotli and precompressed static siblings are opt-in. The existing
transport and JSONL collector continue through the same admission
and request accounting. [Example](../examples/content-and-cache.yaml)
[Architecture decision](adr/0013-content-and-cache.md).

## Rewrite allocations

The Milestone 5 enabled fixture exercised a capture rewrite along with
identity, ACL and rate policies. The isolated same-thread allocator
probe on that operation resolved 100 identical rewrites after one warm-up.
The previous renderer allocated 500 times and requested 29,000 bytes.
The new renderer allocated 200 times and requested 10,000 bytes. It
sizes the result once and transfers the allocation into the HTTP URI
while retaining path and truncation checks. This isolates three fewer
allocations per captured rewrite; it does not quantify whole-request
gateway allocations or speed.
[Before](../bench/program/evidence/milestone-6/rewrite-alloc-before.log)
[After](../bench/program/evidence/milestone-6/rewrite-alloc-after.log).

## Diagnostic and native workloads

The content fixture reuses the open-loop Rust load generator with exact
body and response accounting. It can serve 1 KiB, 64 KiB, 1 MiB, 16 MiB
and 100 MiB static payloads, with a single-range mode. Optional TLS
credentials and protocol selection cover cleartext and TLS HTTP/1.1
and HTTP/2. The runner records binary identities, offer rows, an
independent audit, origin counts and Linux process snapshots.
[Content benchmark runner](../bench/program/content-benchmark.py).

Cache modes cover a warmed single key and an initially cold key. The
Cauce reference uses memory, NGINX uses proxy_cache storage and the
Varnish fixture uses its malloc storage. Use origin-request counts to
confirm the workload. Mixed hit ratios, cache eviction campaigns,
cold filesystem controls, slow readers and compression CPU curves
require independent workload controls. Socket tests cover slow-reader
resource recovery, cache eviction and revalidation. A future disk
implementation needs crash recovery and disk exhaustion gates.

Colima timings are local diagnostics. Qualified throughput, tail latency,
memory, weighted cache eviction and sendfile comparisons remain open.
The static gzip socket fixture verifies decoded equivalence with pinned
NGINX at gzip level 3. Compressor byte output can differ at the same
configured level. [Pinned NGINX cases](../bench/program/evidence/milestone-6/nginx-content.jsonl).

## AWS recovery

STS confirmed the authorized account ending in 6271. EC2 returned
zero reservations by the prior AMD64 instance ID, zero campaign-tagged
volumes and zero network interfaces. The tagged security group, IAM
role and instance profile were deleted. Independent lookups confirmed
their absence. The cost allocation tags were inactive, and Cost
Explorer resource-level data requires payer opt-in. The tag-filtered
zero estimate does not establish actual spend. The remaining
authorized budget cannot be reconciled. This checkpoint provisioned
zero new AWS resources.
[Lifecycle and billing evidence](../bench/program/evidence/milestone-6/aws-reconciliation.json).

## Runner smoke results

On macOS, the 1 KiB static, range, warmed cache and initially cold
cache fixtures each completed 60 measured offers. Each cache fixture
made one origin request over its full run. The generator verified the
range status and delivered bytes. One separate static request verified
the 100 MiB streaming path end to end. These are functional runner
checks at low offered rates.
[Smoke summary](../bench/program/evidence/milestone-6/runner-smoke-darwin.json)
[Raw offers](../bench/program/evidence/milestone-6/runner-smoke-darwin.tar.gz).

## Regression results

Formatting, strict Clippy and 65 Rust tests passed. One optional
microbenchmark was ignored. The original proxy suite passed all 24 cases
for each pool strategy. The content and cache suites passed on both
macOS and Linux ARM64. The compressed wire cases include streaming Brotli,
streaming gzip, precompressed variants and range bypass across those
tests. The pinned NGINX suite passed 15 shared cases. Seven migration
tests and eight YAML examples passed, along with runtime YAML and TOML
checks. The Loom model covered publication against concurrent
invalidation and generation retirement.

The Linux release also passed HTTP/2 and gRPC lifecycle, absolute headers,
HTTP/2 stress, resilience, legacy TLS and WebSocket lifecycle and both
collector suites. Initial runs alongside builds recorded startup and
shutdown timeouts. The same gates passed with Cauce's two compiler jobs
paused. The rejected evidence retains the original results.
[Regression evidence](../bench/program/evidence/milestone-6/regression.tar.gz)
[Rejected fixture runs](../bench/program/evidence/milestone-6/rejected-fixture-runs.tar.gz)
[Source identities](../bench/program/evidence/milestone-6/source-identities.json).

The isolated AddressSanitizer campaigns each completed 100,000
executions, one for the HTTP/2 header observer and one for the content
policy. The latter invokes the production path decoder, range parser and
cache selector encoding. Neither produced a crash artifact.
[Content policy fuzz log](../bench/program/evidence/milestone-6/asan-content-policy.log.gz)
[Header observer fuzz log](../bench/program/evidence/milestone-6/asan-header-observer.log.gz).

Linux ARM64 runner checks accounted for every measured offer in four
105-offer static and warm-cache cells against Cauce and NGINX, and
60 Cauce cleartext HTTP/2 range offers. The sample includes CPU and
memory snapshots and origin counts. Short run length and Colima
prevent release-performance attribution. The Varnish fixture was prepared
and remains unexecuted.
[Linux smoke summary](../bench/program/evidence/milestone-6/runner-smoke-linux.json)
[Linux raw offers](../bench/program/evidence/milestone-6/runner-smoke-linux.tar.gz).

## Gateway allocation diagnostic

Four Heaptrack runs exercised cleartext HTTP/1.1 with one proxy worker
and 100 offers/s. Each accounted for 700 measured and 200 warm-up
successes. The enabled cell exercised trusted-peer processing, a CIDR
allowlist, a capture rewrite, a request zone and the TCP peer cap.
The content handlers and shared cache were absent from those routes.
The allocations include startup and shutdown.

| Runtime and policies | Allocation calls | Calls per success | Peak heap |
| --- | ---: | ---: | ---: |
| Milestone 5, disabled | 24,230 | 26.922 | 406.62K |
| Milestone 5, enabled | 29,342 | 32.602 | 422.06K |
| Milestone 6, disabled | 24,452 | 27.169 | 275.17K |
| Milestone 6, enabled | 27,584 | 30.649 | 485.55K |

The enabled sample had about two fewer allocation calls per success and
a higher peak heap. The disabled sample had 222 more whole-process calls.
Each cell has one Colima repetition. These observations do not establish
a latency, throughput, or disabled-feature allocation effect.
[Allocation summary](../bench/program/evidence/milestone-6/gateway-overhead.json)
[Raw profiles and offers](../bench/program/evidence/milestone-6/gateway-overhead-raw.tar.gz).
