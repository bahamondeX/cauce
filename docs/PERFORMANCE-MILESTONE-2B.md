# Milestone 2B dataplane performance

**Milestone status is open.** The checkout started at `54b2529` and retains the published
`ba6f952` and Milestone 1 `f754e44` commits. The retained dataplane adds
correctness fixes for connection reuse, opening cancellation and generation
draining, plus deterministic pool and JSONL allocation reductions. Native
ARM64 and AMD64 capacity, comparative scaling and the NGINX parity and
superiority gates require qualified hardware.

## Two-worker transport incident

The original 400 requests/s confirmation lost offers 6806–6810. They
dispatched within 54.709 µs and finished within 10 µs of each other. All five
rows have a null HTTP status and only the generator's generic request error.
The first offer waited 20.908 ms between arrival and dispatch. The next
four waited 3.216–3.250 ms. The direct controls passed. This is an unresolved
pre-headers transport failure. The archived recording contains neither the
socket error chain nor a server connection or request ID for any of the five.
[Historical raw offers](../bench/program/evidence/milestone-2/http1-capacity-colima-raw.tar.gz).

With `CAUCE_TRANSPORT_DIAGNOSTICS=1`, the proxy records downstream socket
accept, rejection and termination, request IDs and generation, upstream
leases and reservations, connection-driver completion or drop, permit
release and Hyper error chains. The generator's optional offer tags join
its per-offer monotonic times to proxy events. Worker thread IDs and
connection IDs identify the execution event and the transport involved.
Capture is bounded to 250,000 events per process. Use diagnostic runs for
failure attribution because per-event serialization and I/O affect timing.
The incident runner records dispatch neighbors and the combined lifecycle
for failed offers. Fine-grained pool lock timing and kernel reset packet
capture were outside this replay.

The traced Colima replay at 400 requests/s observed zero transport errors
with one, two and four Tokio workers. At one and four workers it recorded
29 and 17 queue rejections. Scheduled p99 was 152.07, 81.35 and 141.96 ms
respectively. Six direct controls at 800 requests/s passed. In the
four-worker cell the generator and backend shared the guest's proxy
affinity. The replay is diagnostic evidence and the original incident
remains unresolved.
[Replay and correlation data](../bench/program/evidence/milestone-2b/incident-traced-raw.tar.gz).

The previous generator's Reqwest policy disabled its own retries. Hyper's
underlying replay of unstarted requests on reused connections remained
enabled. The generator now configures Hyper directly with pooled replay
disabled. Its scheduler, bounded queue, accounting and payload validation
retain the existing contract. Real-socket tests verified HTTP/1.1 and HTTP/2
over cleartext and TLS, explicit certificate-name rejection and transport
error accounting. Historical client results retain their original generator
identity and retry behavior.
[Hyper pooled retry implementation](https://github.com/hyperium/hyper-util/blob/v0.1.21/src/client/legacy/client.rs#L1595)
[Generator tests](../bench/program/evidence/milestone-2b/no-replay-generator-checks.log).

The final two-worker 400/s replay with pooled replay disabled completed
all 8,000 measured requests and 1,200 warm-up requests. Both direct 800/s
controls passed. Target p99 was 9.21 ms and p99.9 was 117.85 ms. The eight
slowest offers spent 118.7–126.2 ms between dispatch and completion, with
less than 0.5 ms scheduler lag per offer. The cell failed the p99.9 gate.
It did not reproduce the original transport error.
[Final replay and error correlation](../bench/program/evidence/milestone-2b/incident-no-replay-raw.tar.gz).

A deterministic two-worker test confirmed an upstream readiness race
after completion of a response. A local negative control failed when it
skipped waiting for the Hyper sender. At the proxy's request boundary that
upstream send error normally produces a gateway response, so attribution
of the five null-status client rows remains open. A separate regression
confirmed both protocols can previously return opening reservations
without waking pool waiters. The fix attaches notification to reservation
ownership and moves the drain check under the pool lock.
[ADR 0006](adr/0006-upstream-reservation-and-readiness.md)
[Negative control](../bench/program/evidence/milestone-2b/readiness-negative-control.log).

## Dataplane improvements retained

| Commit | Change | Measurable work removed |
| --- | --- | --- |
| `bebfaf8` | Await HTTP/1.1 sender readiness, release opening reservations with notification, gate idle publication during reload and check later eligible HTTP/2 senders | Reproduced dispatch failure and missed wake-up paths |
| `e6f64d5` | Compile numeric pool keys and account for HTTP/1.1 idle sockets under the existing lock | An endpoint-name key copy on acquisition and on reusable HTTP/1.1 return. An idle-list count scan on every ordinary HTTP/1.1 return |
| `2a00647` | Reuse the JSONL writer's event batch and serialization buffer | Batch vector creation per drain and per-record serializer buffer allocation after warm-up |

The protocol suite verifies the Milestone 2 HTTP/2 empty-upload
`END_STREAM` behavior over cleartext and TLS. It also verifies zero-data
upload trailers, GOAWAY replacement, peer stream limits, cancellation,
flow-control stalls and gRPC deadlines. The writer retains the existing
queue bounds and flush cadence, including its ENOSPC and rotation recovery
behavior. [ADR 0007](adr/0007-bounded-dataplane-allocation-reductions.md).

## Native lab and capacity

The new Linux runner builds committed Cauce revisions with a pinned
toolchain, checks role affinity and physical-core separation, and records
environment, release identities, offers, CPU ticks and sampled RSS. The
capacity search raises the direct control as the ramp advances. It refines
between a passing rate and a controlled failure, confirms for at least
60 seconds in native mode, and can run a separate stability hold.

The Colima runner smoke at the start of the campaign rejected the direct
200 requests/s control on scheduler jitter and latency despite accounting
for all 800 measured offers. That keeps this observation out of the
proxy capacity curve.
The final isolated runner smoke passed the 50 requests/s direct control.
The 25 requests/s proxy cell completed all 75 measured offers and exercised
the phase-synchronized sampler. Its 10.162 ms scheduler p99 exceeded the
10 ms qualification gate. The campaign produced no confirmation or scaling
estimate. The earlier Colima fixture's historical one-worker lower bound
remains 399.7 successes/s. Native ARM64 and AMD64 have no
qualified capacity point. Scaling efficiency requires bracketed capacity
for each worker count on the same host and workload.
[Historical curves](../bench/program/evidence/milestone-2b/historical-capacity.png)
[Historical cells](../bench/program/evidence/milestone-2b/historical-capacity.csv)
[Runner smoke and scaling output](../bench/program/evidence/milestone-2b/runner-smoke-raw.tar.gz)
[Native runner and AWS estimate](NATIVE-LINUX-BENCHMARKS.md)
[Benchmark methodology](BENCHMARK-METHODOLOGY.md).

## Pool architecture

| Variant | Idle storage | Production constraints the prototype leaves for integration |
| --- | --- | --- |
| A | Shared lock, indexed endpoint lists with a global count scan as in the base revision | Current production retains a shared lock and uses a constant-time HTTP/1.1 idle count |
| B | Per-endpoint synchronization and an atomic global idle budget | Global pressure eviction, mixed HTTP protocol lifecycle and reload |
| C | A local lane per endpoint and worker, shared overflow, and at most one peer probe | Tokio task migration, peer fairness, connection-open suppression and HTTP/2 stream admission |

The model measures only idle-store operations and uses integer tokens for
sockets. It checks exact token and quota accounting. The isolated quota
primitive also passes a Loom interleaving test. Its comparison uses seven
randomized paired repetitions and a 95% paired bootstrap interval. The
predeclared model threshold is an upper time delta of at most −10% vs
A with exact accounting. A production pool switch requires equivalent
real transport and capacity measurements.

Each cell ran 400,000 idle operations on four guest vCPUs. Times are paired
changes vs A with the bootstrap 95% interval. Lock wait p99 is the median
of per-cell p99 samples in nanoseconds.

| Workload | B time delta | C time delta | Lock wait p99 A / B / C |
| --- | ---: | ---: | ---: |
| One stable endpoint | +8.2% [+5.2%, +20.5%] | +46.1% [+27.9%, +62.0%] | 1,584 / 1,666 / 84 |
| 64 stable endpoints | −30.7% [−34.2%, −27.1%] | −29.1% [−30.0%, −20.1%] | 5,417 / 292 / 84 |
| 64 endpoints, churn | −31.8% [−35.2%, −28.9%] | −28.0% [−31.0%, −23.5%] | 2,166 / 250 / 167 |

All 63 cells passed exact accounting. C's lower sampled lock wait came with
more synthetic opens at 64 stable endpoints than B, with medians of 256
vs 132. B passed the model threshold across the 64-endpoint workloads and
regressed on the single endpoint. Retain the shared production layout.
Prioritize B for a transport-level multi-endpoint experiment on native
hardware. HTTP/2 stream admission, physical connection storms, eviction
fairness, and per-layout memory remain open.
[Raw model cells, acceptance plan and audit](../bench/program/evidence/milestone-2b/pool-layout-raw.tar.gz).

Pingora indexes reusable sockets by group and tracks global capacity
through an LRU and per-group structures. HAProxy checks thread-local
server pools first, then searches eligible thread groups for a compatible
connection that supports takeover. Envoy tracks ready, busy and
connecting clients, plus pending streams, and creates connections from
capacity demand. NGINX uses worker-local cache/free queues and matches
the selected peer address before reuse. The reference designs motivate
the model's separation of scope, locality and global capacity.
[Pinned Pingora pool](https://github.com/cloudflare/pingora/blob/f86ffda5d3ae5c3baf5125682138b33b26c01b08/pingora-pool/src/connection.rs)
[HAProxy 3.4.8 release source](https://www.haproxy.org/download/3.4/src/haproxy-3.4.8.tar.gz)
[Envoy v1.38.0](https://github.com/envoyproxy/envoy/blob/v1.38.0/source/common/conn_pool/conn_pool_base.cc)
[NGINX 1.30.8 keepalive](https://github.com/nginx/nginx/blob/release-1.30.8/src/http/modules/ngx_http_upstream_keepalive_module.c).

## CPU and allocations

The Colima captures use the retained binary with release optimization and debug
line tables. The HTTP/1.1 captures ran for 60 seconds at 399 Hz with a separate
sampler CPU. Their enclosing load runs validated 15,750 measured responses in
each logging mode. The logs-off capture has 482 CPU samples and the logs-on
capture has 836. The earlier Milestone 2 runs contained 34 and 52 samples. The
following table ranks known leaf symbols by sample count. The counts measure
user-space sampled CPU time during the diagnostic window.

| Rank | Logs off | Samples | Logs on | Samples |
| --- | --- | ---: | --- | ---: |
| 1 | Proxy request handler | 23 | JSONL writer thread closure | 39 |
| 2 | Upstream driver task | 13 | Proxy request handler | 29 |
| 3 | Standard header-name decoding | 12 | Standard header-name decoding | 25 |
| 4 | Tokio task polling | 12 | Tokio task polling | 22 |
| 5 | Tokio Notify polling | 12 | JSON string serialization | 17 |

The captures also contain 70 and 122 samples with unknown leaf symbols.
Release inlining leaves several operations within the request handler and driver
closures. In particular, the samples do not isolate configuration snapshot
locking, Arc traffic or Tokio's timer registrations. This ranking prioritizes
logging and header processing for focused measurements. It leaves the request
snapshot, balancing and timer design decisions open. The HTTP/2 cleartext
capture contains 593 samples at the same frequency over 35 seconds. Its
leading known leaves were the h2 connection poll (27) and header block
encoding (20), followed by the proxy handler (15). The H2 task executor
closure accounted for nine leaf samples.

Separate 18-second HTTP/1.1 counter passes recorded the following values.

| Counter | Observation |
| --- | ---: |
| Task clock | 724.62 ms |
| Context switches | 9,277 |
| CPU migrations | 0 under single-CPU affinity |
| Syscall entries | 32,519 |
| futex entries | 302 |
| epoll_pwait / epoll_ctl / epoll_pwait2 entries | 9,490 / 16 / 0 |
| hrtimer start tracepoint events | 10,689 |
| Scheduler wake-up tracepoint events | 294 |

Cycles, instructions, branches, branch misses, L1 and LLC cache misses are
explicitly unsupported in this guest. IPC is unavailable. The syscall pass
corresponds to about 7.2 syscall entries per scheduled offer over its nominal
18-second overlap at 250/s. Both counter passes cover a subset of the verified
request window. Thread-scoped tracepoints count events observed on the
selected tasks. They leave off-CPU lock wait durations, user-space timer
registrations and atomic reference-counting costs unattributed.
[Software counters, stacks and raw load](../bench/program/evidence/milestone-2b/software-profiles-raw.tar.gz)
[All measured leaf symbols](../bench/program/evidence/milestone-2b/software-profile-rank.csv).

Heaptrack instrumented a single HTTP/1.1 logs-on pair at 100/s using
symbolized builds from the concurrency fix and the retained runtime. Each
process produced 2,100 successful responses across warm-up and measurement,
with 2,100 unique JSONL records. Counts cover the whole process, including
startup and shutdown. Cumulative bytes sum allocation size times frequency
over the raw allocation histogram.

| Build | Allocation calls | Cumulative bytes | Calls per success | Bytes per success | Peak heap |
| --- | ---: | ---: | ---: | ---: | ---: |
| Concurrency fix `bebfaf8` | 125,978 | 46,508,419 | 59.99 | 22,147 | 2.23 MB |
| Retained runtime `2a00647` | 110,984 | 39,958,723 | 52.85 | 19,028 | 2.42 MB |

The retained build had fewer allocation calls and cumulative allocated bytes
in this diagnostic pair. Peak heap was higher. The separate source changes
deterministically eliminate key copies and buffer initialization. Native
paired CPU, latency and memory comparisons remain open. An earlier 250/s
heaptrack attempt recorded one null-status pre-headers EOF on the
one-worker concurrency-fix build, with the generator reporting
`connection closed before message completed`. That run was rejected as a
comparison and supplies no causal link to the historical five-error burst.
[Allocation histograms, traces and offers](../bench/program/evidence/milestone-2b/allocation-profiles-raw.tar.gz)
[Allocation accounting](../bench/program/evidence/milestone-2b/allocation-summary.json).

## Competitive benchmark matrix

All cells in the release comparison require matching admission, connection
limits, retries, timeout, buffering and backpressure semantics. TLS cells
also require matching trust, protocol and session policies. Logs-on cells
require equivalent fields, buffering, event-loss and durability policies.
The Milestone 2 NGINX access format and Cauce JSONL contract differ in these
respects, so their historical CPU and cgroup figures describe the fixtures.

| Workload | Retained Cauce | NGINX OSS 1.30.8 | HAProxy 3.4.8 fixture | Pingora 0.9.1 fixture |
| --- | --- | --- | --- | --- |
| HTTP/1.1 cleartext | Local socket checks and Colima diagnostic replay | Historical 250/s characterization with `ba6f952` and `f754e44` | Echo and overload characterization | Echo and overload characterization |
| HTTP/1.1 TLS | Linux ALPN, verified upstream TLS, reload, rotation and deadline checks passed | Pending pinned equivalent fixture | Outside the cleartext fixture | Outside the cleartext fixture |
| HTTP/2 cleartext and TLS | Protocol, lifecycle and stress gates passed | Pending pinned comparison. The older campaign uses 1.30.6 | Outside the cleartext fixture | HTTP/1.1 pinned upstream |
| gRPC unary and streaming | All four RPC shapes verified in the Linux test suite | Pending pinned comparison. The older gRPC campaign uses 1.30.6 | Outside the cleartext fixture | Outside the bodyless GET fixture |
| SSE | Real-socket streaming checks passed | Pending paired workload | Pending | Pending streaming fixture |
| WebSocket | Linux plain/TLS upgrades, fragmentation, bidirectional traffic, idle and shutdown checks passed | Pending paired workload | Pending | Outside the bodyless GET fixture |
| Large upload and download, slow clients | Body bounds, early upload response, downstream stall and deadline checks passed | Pending paired workload | Pending | Upload outside bodyless GET fixture |
| Multiple upstreams and connection churn | Burst, reload, disconnect and idle retirement exercised | Pending paired workload | Single backend fixture | Single backend fixture |
| Overload and recovery | Socket and Linux resilience suites passed | Milestone 2 normal-workload admission gate | Reference server connection queue | Reference application semaphore |

Both reference fixtures recovered after the 32-request hold. HAProxy queued
the excess request for 554 ms before returning 200. Pingora returned 503
after 48 ms. The timings describe the overload policy on the Colima
fixture. [Reference admission probe](../bench/program/evidence/milestone-2b/reference-admission-raw.tar.gz).

The matched-rate Cauce source set remains `ba6f952`, `f754e44`, `54b2529`
and the retained 2B candidate. The older three-way data covers the first
two Cauce revisions and NGINX. Native matched-rate and saturation cells,
equivalent access logging and competitive release statistics remain open.
[Milestone 2 history](PERFORMANCE-MILESTONE-2.md)
[Reference fixture setup](../bench/program/references/README.md).

## Regression and release gates

| Gate | Evidence |
| --- | --- |
| Rust format, strict Clippy and all-target tests | Format and Clippy passed on Darwin ARM64. All-target tests passed on Darwin ARM64 and Linux ARM64 in Colima, with 46 passed and one ignored route microbenchmark on each |
| Real sockets, YAML and TOML, reload and rollback | 23 real-socket cases and both configuration examples passed on Darwin ARM64. The focused burst, backend EOF and retirement checks also passed with 1, 2 and 4 workers |
| Collector | 23 integration and 13 storage cases passed on Darwin ARM64 |
| Linux HTTP/2, gRPC and absolute header deadlines | Release image passed cleartext and TLS protocol, lifecycle, stress and absolute-header suites in Colima |
| Linux TLS and WebSocket | Certificate rotation, trust replacement, handshake deadlines, 4 MiB echo, mixed plain/TLS hops and tunnel shutdown passed in Colima |
| Linux resilience | Proxy failure, log-disk ENOSPC and collector storage suites passed in Colima |
| AddressSanitizer | Linux ARM64 header observer fuzzed 100,000 inputs with seed 60616. Zero crash artifacts |
| Native ARM64 and AMD64 | Qualified hosts pending. Colima profiles are Linux ARM64 VM diagnostics |
| Competitive parity and target superiority | Open. Neither capacity scaling nor equivalent competitor timings has qualified on native hardware |

The dataplane remains the active milestone. Future runtime, allocator, TLS
provider, LTO, PGO and I/O experiments require isolated qualified samples.
The observed retirement and dispatch fixes provide no basis for replacing
Tokio's scheduler, rewriting the HTTP/2 deadline machinery or promoting a
new pool layout.

[Darwin checks](../bench/program/evidence/milestone-2b/darwin-regression.log)
[Linux raw verification](../bench/program/evidence/milestone-2b/linux-regression-raw.tar.gz)
[Linux all-target tests](../bench/program/evidence/milestone-2b/linux-all-targets-arm64.log)
[TLS checks](../bench/program/evidence/milestone-2b/linux-tls-lifecycle-final.log)
[WebSocket checks](../bench/program/evidence/milestone-2b/linux-websocket-edges-final.log)
[ASan raw log](../bench/program/evidence/milestone-2b/asan-header-observer.log.gz)
[Evidence checksums](../bench/program/evidence/milestone-2b/SHA256SUMS).
