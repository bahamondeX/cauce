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
