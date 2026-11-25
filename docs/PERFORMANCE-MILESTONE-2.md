# Milestone 2 dataplane performance

## Source and build identity

The published reference is `ba6f95277f5ffca966faea5e25203c1b75d310d5`.
The Milestone 1 tree is `1e937190468c2f366912fa16ad24325eb7d79073`,
checkpointed in `f754e44eb878c89281797c33203f0dd49885d75c`.
The Cauce release builds use Rust 1.88, thin LTO, one codegen unit and symbol
stripping. They run on Debian Bookworm glibc 2.36 with the system allocator and
rustls with ring. NGINX 1.30.8 comes from the pinned upstream Alpine image. Its
C compiler options, musl, system allocator and OpenSSL linkage remain distinct.
The three-way workload uses HTTP/1.1 cleartext on both hops. The Dockerfile build ran through BuildKit on
Colima Linux ARM64. Checkpoint output matched after a clean Cauce package
rebuild. The telemetry reference reproduced the prior cache bug and generated
a distinct binary after package invalidation. [Build identities](../bench/program/evidence/milestone-2/build-identities.txt) [Cache decision](adr/0004-docker-release-cache.md).

## Generator and access telemetry

The first three-way Python run at 250 offers/s dropped 11 of 2,000 offers in
one cell and reached 214.88 ms scheduler p99 there. It was rejected.
The first Rust experiment at 600 target offers/s rejected its 1,200 offers/s
direct control after 74 warm-up queue rejections. The next Rust experiment kept
the 10 ms scheduler p99 gate and used 250 target offers/s and 500 control
offers/s with separate guest CPU affinities for proxy, backend and generator.
Both controls finished every measured and warm-up offer. Their scheduler p99
was 1.59 and 1.00 ms. Seven of 36 target cells were rejected on scheduler jitter.
All 45,000 target offers completed successfully across the full set.

The independent TLS echo smoke test on Darwin ARM64 verified HTTP/1.1 and
HTTP/2, including certificate and IP validation, with 50 of 50 successful
measured replies per protocol. HTTP/2 exceeded the scheduler gate at 10.27 ms,
and a separate Colima HTTP/1.1 TLS check exceeded it at 10.19 ms. These are
protocol checks. The two contaminated timings remain rejected.
[Darwin TLS offers](../bench/program/evidence/milestone-2/load-tls-darwin-protocol-smoke.tar.gz)
[Colima TLS rejection](../bench/program/evidence/milestone-2/load-tls-linux-rejected.tar.gz).

| Logs | Valid pairs | Reference median CPU µs/success | Checkpoint median CPU µs/success | Paired median CPU delta | Bootstrap 95% interval |
| --- | ---: | ---: | ---: | ---: | --- |
| Off | 8 | 212 | 208 | −20 | −40 to +32 µs |
| On | 4 | 212 | 248 | +28 | Insufficient valid pairs |

Logs-off median scheduled p99 rose from 5.39 to 9.28 ms across valid pairs.
The CPU interval spans zero. These results leave the telemetry CPU effect
inconclusive and identify a tail-latency difference to recheck on isolated
hardware. The binaries came from normal Dockerfile builds and ran directly
on the same Ubuntu guest libc. This comparison is VM-scoped.
[Raw telemetry archive](../bench/program/evidence/milestone-2/telemetry-native-raw.tar.gz)
[Paired summary](../bench/program/evidence/milestone-2/telemetry-native-summary.json)
[Rejected Python run](../bench/program/evidence/milestone-2/three-way-python-rejected.tar.gz).

## Three-way fixed-rate baseline

The normal HTTP/1.1 run completed 18 cells and 36,000 successful requests, with
zero missing offers, drops, queue rejections, transport errors or jitter-rejected
cells. Both direct controls qualified at 500 offers/s and all 18 application
admission checks held 32 backend requests and returned one excess 503. All nine
logs-on cells passed exact access-record accounting. The proxy budget was one
guest CPU, 256 MiB, 32 active requests and at most 32 observed backend connections.
None of the runs exercised TLS. Compiler, libc, telemetry and idle downstream
socket semantics differ between stacks.

Each row is the median of three randomized 8-second measurements after a
3-second warm-up at 250 offers/s. Observed goodput was 250 successful requests/s
for every cell. Treat resource and tail differences as descriptive. Three
repetitions are below the optimization claim gate.

| Stack | JSONL | CPU µs/success | p50 ms | p95 ms | p99 ms | p99.9 ms | RSS MiB | Sampled cgroup peak MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Published Cauce | Off | 170.4 | 1.56 | 2.59 | 9.56 | 25.60 | 5.88 | 2.55 |
| Milestone 1 checkpoint | Off | 165.8 | 1.60 | 2.66 | 13.19 | 45.79 | 6.00 | 2.53 |
| NGINX | Off | 144.3 | 1.47 | 2.52 | 15.18 | 34.55 | 9.03 | 3.03 |
| Published Cauce | On | 205.3 | 1.58 | 2.61 | 13.44 | 44.62 | 7.88 | 6.51 |
| Milestone 1 checkpoint | On | 198.0 | 1.57 | 2.58 | 11.26 | 35.66 | 8.13 | 6.55 |
| NGINX | On | 152.8 | 1.09 | 1.85 | 14.31 | 29.81 | 9.94 | 3.78 |

RSS sums proxy processes and can count shared pages twice. Cgroup memory uses
the Docker Engine's sampled memory usage field, which can apply a different
cache treatment from process RSS. The combined checkpoint effects include
routing, route ownership, reload preparation, telemetry and collector changes
in the source tree. This workload includes no reload or collector activity. The
one-route workload does not isolate the route matcher from ownership changes.
The prior lookup-only campaign covers 1, 16, 128 and 1,024 routes.
[Audited summary](../bench/program/evidence/milestone-2/three-way-native-summary.json)
[Raw three-way archive and rejected adapter run](../bench/program/evidence/milestone-2/three-way-native-raw.tar.gz)
[Lookup-only measurements](PERFORMANCE.md).

## HTTP/1.1 capacity and worker allocation

The cleartext echo ramp used the checkpoint, logs off, eight-second cells at
100, 250 and 400 offers/s, three seconds of warm-up and a 20-second confirmation.
The SLO required p99 ≤20 ms, p99.9 ≤100 ms, offer-window goodput ≥99% and zero
errors. The backend and client were pinned to separate guest CPUs. Both rounds
used direct controls at 800 offers/s before and after the ramps.

| Proxy CPUs and Tokio workers | Highest ramp rate | Confirmation p99 / p99.9 | Successful / offered | Offer-window goodput | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| CPU 0, one worker | 400/s | 7.40 / 23.37 ms | 8,000 / 8,000 | 399.70/s | Confirmed |
| CPUs 0 and 3, two workers | 400/s | 8.14 / 36.37 ms | 7,995 / 8,000 | 399.75/s | Failed zero-error SLO |

The five transport errors in the two-worker round occurred on consecutive
offers within one short dispatch burst. The client log lacks the underlying
transport error chain, so a causal attribution remains open. Both direct
controls passed. The one-worker result is a confirmed lower bound over the
rates tested. The campaign did not reach a qualifying saturation point or
establish multicore scaling efficiency. A repeat with expanded transport
error diagnostics completed all 800 offers at the initial 100/s cell. Its
29.86 ms p99 exceeded the 20 ms SLO, so the runner stopped the ramp. The
original transport error remains unresolved.
[Capacity run and rejected two-worker confirmation](../bench/program/evidence/milestone-2/http1-capacity-colima-raw.tar.gz)
[Two-worker diagnostic retry](../bench/program/evidence/milestone-2/http1-capacity-2worker-diagnostic.tar.gz).

## Linux profiling and experiment selection

The guest has four allocated AArch64 vCPUs and the `arch_sys_counter` clock
source. Linux `perf` software events work with a scoped privileged invocation.
Cycles and instructions returned `<not supported>` and sysfs reports no hardware
PMU. The guest supplies scheduling and process-memory data through `/proc` and
cgroup v2. The symbolized checkpoint used the release profile with added debug
line tables and symbol stripping disabled.
[Guest environment](../bench/program/evidence/milestone-2/colima-linux-arm64-replay-environment.txt)
[Perf capability check](../bench/program/evidence/milestone-2/perf-qualification.log).

| Workload | CPU-clock samples | Observed stack evidence |
| --- | ---: | --- |
| HTTP/1.1, JSONL disabled | 34 | Single-worker cleartext echo. [Flamegraph](../bench/program/evidence/milestone-2/http1-logs-off-colima-arm64.svg) |
| HTTP/1.1, JSONL enabled | 52 | Proxy and logging workload captured on a single worker. [Flamegraph](../bench/program/evidence/milestone-2/http1-logs-on-colima-arm64.svg) |
| HTTP/2 cleartext, JSONL disabled | 77 | Overlapping groups of 19 samples with the HTTP/2 task executor, 12 with the request handler, and 3 with the upload path. [Flamegraph](../bench/program/evidence/milestone-2/http2-clear-colima-arm64.svg) |
| gRPC unary, cleartext | 35 | 2,100 validated RPCs. Overlapping groups of 11 samples with the stream executor, 3 with uploads and 2 with timer symbols. [Flamegraph](../bench/program/evidence/milestone-2/grpc-unary-colima-arm64.svg) |
| gRPC bidi, cleartext | 43 | 2,100 validated RPCs and 6,300 response messages. Overlapping groups of 11 samples with the stream executor and 3 with uploads. [Flamegraph](../bench/program/evidence/milestone-2/grpc-bidi-colima-arm64.svg) |

Samples count CPU-active stack observations. This capture leaves off-CPU lock
waiting, timer registration frequency, allocation bytes and atomic contention
unquantified. The pool source contains a shared idle lock and a connection
sorting path, and the balancing cursor is shared within each generation. These
are candidates for multi-endpoint measurements. Pingora groups deadlines at
10 ms resolution and offers lazy timer initialization. Cauce's existing stream
watchdogs enforce absolute limits even during a flow-control stall. A timer
substitution would need separate measurements and absolute-deadline tests.
[Perf raw capture](../bench/program/evidence/milestone-2/linux-profiles-raw.tar.gz)
[gRPC stacks and validated offers](../bench/program/evidence/milestone-2/grpc-profiles-colima-raw.tar.gz)
[Pinned Pingora timers](https://github.com/cloudflare/pingora/blob/f86ffda5d3ae5c3baf5125682138b33b26c01b08/pingora-timeout/src/lib.rs).

The 10-second HTTP/2 software-counter sample recorded 723.4 ms of task-clock,
8,062 context switches and two page faults. Zero CPU migrations reflects the
single-CPU affinity in that sample. The profiler window covers part of the
generator window, so the snapshot does not provide a CPU-per-request estimate.
[Counter values](../bench/program/evidence/milestone-2/http2-linux-perf-stat.csv)
[Follow-up CPU stacks and snapshots](../bench/program/evidence/milestone-2/linux-profiles-followup-raw.tar.gz).

## HTTP/2 empty upload experiment

The checkpoint put a request already at end-of-stream through the upload guard.
The guarded body allocated a sleep and exposed a live body to Hyper until
polled. The experimental build passes an empty body straight to the upstream
HTTP/2 sender when Hyper confirms there are no pending frames. The Node
backend observed `END_STREAM` on the initial HEADERS over cleartext and TLS.
The checkpoint failed the same assertion. The candidate also passed the
trailer-only upload check and the existing gRPC, flow-control, cancellation
and active-request lifecycle suites.
[Checkpoint negative control](../bench/program/evidence/milestone-2/http2-eos-checkpoint-rejected.log)
[Candidate socket tests](../bench/program/evidence/milestone-2/http2-eos-candidate.json).

The bodyless HTTP/2 echo campaign ran 36 cells at 200 offers/s. All 36,000
requests succeeded. Direct controls passed at 400/s. One cell exceeded the
10 ms scheduler p99 gate and was excluded from paired analysis.

| Logs | Valid pairs | Paired median CPU delta (95% CI), µs/success | Paired median p99 delta (95% CI), ms |
| --- | ---: | ---: | ---: |
| Off | 9 | −10 (−40 to +20) | +4.18 (−13.04 to +6.65) |
| On | 8 | +5 (−30 to +30) | −5.97 (−8.74 to +4.65) |

All four intervals include zero. The change removes one terminal HTTP/2
frame in the measured empty-request exchange and skips the unused upload
timer. The VM campaign leaves CPU and tail-latency savings inconclusive.
[Paired metrics](../bench/program/evidence/milestone-2/http2-empty-ab-colima-summary.json)
[Raw HTTP/2 data](../bench/program/evidence/milestone-2/http2-empty-ab-colima-raw.tar.gz).

## HAProxy and Pingora references

HAProxy 3.4.8 built from its checksum-verified release tarball with GCC
12.2.0 and no TLS provider enabled. Pingora 0.9.1 commit `f86ffda` built
with Rust 1.88 and an isolated Cargo lockfile, also for cleartext. Each
proxied and validated 100 of 100 measured HTTP/1.1 echo offers with one
worker and separately pinned client and backend containers. This establishes
fixture operation. Equivalent physical connection accounting, saturation,
TLS and sustained competitive results remain open.
[Build and smoke reproduction](../bench/program/references/README.md)
[Raw reference smoke checks](../bench/program/evidence/milestone-2/reference-smoke-raw.tar.gz).

## Regression and performance gates

| Gate | Status | Scope |
| --- | --- | --- |
| Dockerfile rebuild and source isolation | Passed | Default BuildKit, Linux ARM64 VM. Distinct source produced distinct proxy binaries. |
| Exact offer accounting and qualification | Passed for the replay | Three-way native HTTP/1.1 and valid telemetry and HTTP/2 pairs. Rejected cells remain in the archives. |
| Released Cauce, checkpoint and NGINX | Characterized | One cleartext route, 250/s, three repetitions, Colima. |
| Route matcher, cloning, async reload | Partly characterized | Lookup-only data exists for 1, 16, 128 and 1,024 routes. Separate HTTP comparisons for matcher, route cloning and live reload remain open. |
| Access telemetry | Inconclusive | Eight valid logs-off pairs and four logs-on pairs. |
| Empty upstream HTTP/2 framing | Passed | Cleartext and TLS, independent Node peer, trailer-only branch exercised. |
| Empty upstream HTTP/2 efficiency | Inconclusive | Fixed-rate CPU and tail intervals include zero. Wire framing reduction verified. |
| CPU and memory profiling | Partial | Linux software CPU stacks and counters. Allocation, lock waits, per-request hardware counters and the full workload matrix remain open. |
| HTTP/1.1 capacity | Partial | One-worker lower bound confirmed. Two-worker qualification failed. |
| HAProxy and Pingora reference fixtures | Smoke passed | Pinned releases, cleartext echo only. Competitive equivalence and capacity remain open. |
| Native Linux multicore and competitor capacity | Pending | Colima has four vCPUs and lacks a hardware PMU. Native Linux ARM64 and AMD64 runs require suitable hardware access. |
| Format, clippy, all-target Rust tests | Passed | Darwin ARM64 and the release source. 44 Rust tests. |
| Real sockets, reload/rollback, YAML/TOML, collector | Passed | 22 integration tests, 23 collector cases and 13 collector storage cases. Config examples and schema validated. |
| HTTP/2 and gRPC, stress, absolute headers, resilience | Passed | Candidate normal-Dockerfile build, Linux ARM64 VM. |
| Seeded header observer fuzzing | Passed | 100,000 address-sanitized executions. Observer, target and lockfile matched the source bytes. |

[Darwin regression log](../bench/program/evidence/milestone-2/candidate-regression-darwin-arm64.log)
[Linux HTTP/2, fuzz and resilience evidence](../bench/program/evidence/milestone-2/http2-eos-linux-regression.tar.gz)
[Raw file checksums](../bench/program/evidence/milestone-2/SHA256SUMS).

ArcSwap, endpoint pool alternatives, timer aggregation, worker-affinity runtime
designs, TLS providers, allocators and PGO remain separate experiments. The VM
results leave the program's CPU, tail latency, sustainable goodput, multicore
efficiency and memory targets unverified.
