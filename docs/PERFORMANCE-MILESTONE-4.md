# Milestone 4 gateway checkpoint

## AWS lifecycle

The control plane still requires authentication to the authorized account
ending in `6271`. The AWS Core connector returned a reauthentication
requirement and the local SDK reported `LoginRefreshRequired`. The
browser sign-in selected account ending in `5288`.
The profile overwrite was declined, preserving the existing profile.
EC2 termination, EBS and interface removal, the temporary security group
and IAM role cleanup, and billed expenditure remain unverified on AMD64.
The prior **$2.14** figure remains a list-rate estimate with reserves.
Milestone 4 provisioned no resources, because the previous lifecycle and
remaining allowance still need reconciliation through AWS.
[Milestone 3 lifecycle](../bench/program/evidence/milestone-3/aws-lifecycle.json)
[Milestone 4 recovery evidence](../bench/program/evidence/milestone-4/aws-recovery.json).

The local ARM64 bundle includes Cauce `ba6f952`, `47da8fe`, and
`168ed82`, plus pinned NGINX 1.30.8, HAProxy 3.4.8, Pingora 0.9.1, Node
and the open-loop generator. The runner prioritizes the current Cauce and
NGINX comparison. It checksums both historical binaries, and each runner
keeps the existing qualification gates. No new native ARM64 or AMD64 run
qualified during this checkpoint.
[ARM64 artifact manifest](../bench/program/evidence/milestone-4/arm64-stage.json).

## AMD64 capacity boundary review

The Milestone 3 native run compared `47da8fe` with NGINX. At 1,000
requests/s with one worker and access logs disabled, Cauce used 40 µs of
CPU per success versus 46 µs for NGINX across seven qualified pairs. Its
median p99 was 152.90 µs versus 147.03 µs. This matched-rate measurement
did not test saturation.

The independent auditor rechecked **8,487,500** measured offers across
the primary and supplemental archives. The source review traced
`queue_rejection` to the open-loop generator's bounded dispatch queue
before an upstream exchange starts. Sustained service delays can
contribute to queue pressure, so a queue rejection identifies where an
offer was lost and leaves the underlying cause open.

| Cell | Raw observation | Interpretation |
| --- | --- | --- |
| Cauce one worker, 7,500/s | 450,000 measured and 15,000 warm-up successes. The CPU mean was about 0.25 core equivalents. | Qualified 60-second lower bound. |
| Cauce one worker, 8,000/s | All 40,000 measured offers succeeded. The warm-up rejected 237 offers between 342 and 371.5 ms on the schedule. | Rejected end-to-end. The experiment does not attribute a steady-state ceiling. |
| Cauce two workers, 15,000/s | All 900,000 measured offers succeeded. The 32,000/s direct postflight lost 1,246 offers in warm-up. | Postflight rejected the bracket. |
| Cauce four workers, 16,000/s | 42 generator queue rejections in a roughly 2.6 ms scheduled interval. | Confirmation rejected. |
| Cauce eight workers, 8,000/s | Passed confirmation. At the next step the direct backend control had 1,960 warm-up queue rejections at 32,000/s. | Control-limited lower bound. |
| NGINX one worker, 8,000/s | 480,000 successes with both controls passing. An earlier 16,000/s run lost 14 measured offers. | Qualified 60-second lower bound. |

The measured one- and eight-worker Cauce results are lower bounds and
leave scaling efficiency open. The data do not isolate connection
admission, pooling, Tokio scheduling, socket readiness, or a backend
throughput ceiling. The load generator uses a dedicated offer-clock
thread and a single-threaded Tokio dispatcher. Adding more CPUs to its
affinity alone does not change that execution model. A future native
capacity run needs independently qualified headroom and transport,
scheduler and queue samples at a matched rate and then near the first
service-limited cell. The admission and retry policies should remain
matched across proxies.
[Reclassified capacity CSV](../bench/program/evidence/milestone-4/amd64-capacity.csv)
[Scaling evidence](../bench/program/evidence/milestone-4/amd64-scaling.csv)
[Capacity and scaling figure](../bench/program/evidence/milestone-4/amd64-capacity-and-scaling.svg)
[Original offer archives and source revisions](../bench/program/evidence/milestone-3/raw-archives.json).

The runner continues to reject startup loss and generator queue loss. It
now independently checks warm-up timestamps, response status and payload
length and records warm-up outcomes separately. A rejected warm-up or
generator-queue cell can guide rate refinement while leaving the reported
saturation boundary unattributed. The re-auditor applies this classification
to archived capacity cells.

## Dataplane change

The path validator now checks percent escapes without first allocating and
lowercasing a copy of the request path. A focused Rust regression covers
all 256 octets in both hex cases and verifies chained and incomplete
escapes. A standalone Darwin ARM64 source-level probe counted **one
allocation before and zero after** per call for both ordinary and escaped
path samples. It ran nine randomized pairs of 262,144 calls per version
and workload. Its counting allocator and local host limit timing claims to
the probe. The raw results preserve CPU timing and the source of each
version. [Probe evidence](../bench/program/evidence/milestone-4/path-probe.tar.gz).

## Local feature cost diagnostic

The four-vCPU Linux ARM64 Colima guest ran three randomized blocks at
500 HTTP/1.1 offers/s. Each two-second target cell used one worker and
twice-rate direct controls. Both features used the same fixed-size
backend response. All test proxy processes exited after measurement.

| Variant | Cells passing all gates | Median goodput/s | CPU µs per success | p99 ms | RSS MiB | PSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `168ed82` | 1/3 | 500.0 | 100 | 5.302 | 4.875 | 3.570 |
| Milestone 4 with both features disabled | 1/3 | 500.0 | 90 | 1.722 | 5.000 | 3.754 |
| Milestone 4 with one regex route | 2/3 | 500.0 | 90 | 3.096 | 5.688 | 4.379 |
| Milestone 4 with one request and one response header edit | 2/3 | 499.5 | 125 | 10.308 | 5.250 | 4.018 |

Only the first block passed as a complete paired comparison. The local
results leave the feature overhead gate open. The archived raw offers and
controls preserve the rejected cells and include p50, p95 and p99.9.
[Local diagnostic evidence](../bench/program/evidence/milestone-4/local-feature-diagnostic.tar.gz).

## Regression gates

Formatting, strict Clippy, 56 Rust tests, 24 real-socket checks under each
pool strategy, four migration tests, and 13 Linux harness checks passed.
The Linux release image passed HTTP/2 and gRPC lifecycle and stress,
TLS, WebSocket, and all three resilience suites. Collector validation
passed 23 functional and 13 storage cases. The same HTTP/2 observer
inputs ran 100,000 AddressSanitizer fuzz executions without a crash.
The evidence retains the earlier failed failover and WebSocket timing
runs alongside their successful isolated reruns.
[Regression archive](../bench/program/evidence/milestone-4/regression.tar.gz)
[TLS and WebSocket evidence](../bench/program/evidence/milestone-4/tls-websocket.tar.gz)
[Sanitizer evidence](../bench/program/evidence/milestone-4/asan-h2-headers.log.gz).

## Feature scope and remaining gates

The functional increments add flat location precedence and bounded
header edits. Their differential tests exercise the shared NGINX subset.
The production pool default is compact. Weighted balancing, additional
traffic policies, TLS lifecycle expansion and static serving/caching
remain part of the compatibility backlog.

A successful replay remains insufficient to explain the historical
transport failures. The existing bounded diagnostics and client retry
policy were preserved. Native allocation profiling, competitive SLO
capacity and tests of reuse-port or multiple accept loops remain open
pending a suitable host.
[Functional parity](FUNCTIONAL-PARITY.md).
