# Milestone 2C · Native performance and endpoint pooling

The candidate starts from `2619351` and keeps the compact pool as the default.
It removes request-path allocations in header handling and access events, adds
transport-stage diagnostics, and makes endpoint sharding available as an opt-in
experiment. Native release-performance gates remain blocked. The existing AWS
credential mechanism authenticated, and the inventory found no reusable hosts.
Launching the proposed hosts needs explicit authorization.

## Allocation reductions

The proxy now reuses typed standard header names and compiles the upstream Host
value with each endpoint. It formats the forwarded IP once per downstream
connection. It forwards the same request ID bytes upstream and downstream.
Dynamic Connection-nominated headers still require parsing. The header tests
cover multiple nominations, case normalization, and nominated TE removal.

The logger stores fixed fields as static values. It captures the event timestamp
at event creation and formats it into a stack buffer in the writer. The UUID
uses another stack buffer. A wire-format test checks the RFC 3339 and UUID
strings. The collector contract and event accounting passed the local suites.

The logs-on HTTP/1.1 Heaptrack replay used 100 offers/s, one worker, a
1 KiB response, 200 warm-up requests and 1,900 measured requests. All three
processes served 2,100 successes and recorded exactly 2,100 unique events.
Whole-process allocation counters include startup and shutdown.

| Build | Allocations per success | Allocated bytes per success | Peak heap |
| --- | ---: | ---: | ---: |
| 2B comparison base `bebfaf8` | 59.74 | 20,849 | 2.39 MB |
| Retained 2B producer `2a00647` | 53.05 | 19,889 | 2.47 MB |
| 2C compact candidate | 34.77 | 18,413 | 2.16 MB |

The 2B repeat shows 11.2% fewer allocation calls with higher peak heap. The
earlier run measured 11.9% fewer calls and a larger peak increase. In this
repeat the 2C candidate used 34.5% fewer calls and 7.4% fewer cumulative bytes
per success than 2B. The candidate used the stripped runtime and the two
comparison builds were symbolized. The recorded peak heap was 2.16 MB in
that diagnostic run. No per-change attribution or production CPU, latency,
or memory claim follows from this single guest capture.
[Raw Heaptrack, logs and offers](../bench/program/evidence/milestone-2c/allocation-raw.tar.gz)
[Independent raw accounting](../bench/program/evidence/milestone-2c/raw-audit.json)
[Original 2B measurements](PERFORMANCE-MILESTONE-2B.md#cpu-and-allocations).

## Transport incident and tail latency

The five original pre-headers failures remain unresolved. Their original raw
client rows have no HTTP status, request ID, local socket port or error chain.
The earlier one-worker Heaptrack failure recorded a pre-headers unexpected EOF,
with no corresponding proxy socket trace. A link between these incidents and
the corrected sender-readiness race remains unestablished.

The replay now links generator offers to proxy requests by offer ID, and to
accepted downstream sockets by local port when the proxy did not parse a
request. It records request receipt, routing, pool acquisition, sender readiness,
Hyper dispatch, connection write and read activity, response headers, body
completion, downstream handoff and flush. Records include process-relative
monotonic times, runtime thread identifiers, request IDs and scoped connection IDs.
The Linux diagnostic sampler records TCP socket queue and retransmission fields
from `ss` and thread scheduling counters from `/proc`. Event serialization
and sampling are confined to separate diagnostic runs.

The two-worker diagnostic replay served all 8,000 measured and 1,200 warm-up
offers at 400/s without a transport error. Direct 800/s controls passed.
The bounded capture accepted 165,873 records with zero dropped. The replay
recorded p99 of 5.75 ms and p99.9 of 64.88 ms, compared with 9.21 ms and
117.85 ms in the previous replay. Five socket EOFs did not coincide with a
failed request. The trace also recorded HTTP/1.1 downstream header timeout
closures outside the failed-request set, which was empty.

The twelve slowest scheduled responses clustered within one dispatch burst.
Acquisition took 0.003–0.023 ms and sender readiness took at most 0.006 ms
across those requests. Eleven had less than 10 ms between the service starting
and the matched downstream flush. Offer 4885 spent 68.42 ms between client
dispatch and completion, including 31.21 ms from request start to downstream
flush. Most of that internal interval was before the final upstream headers.
The largest remaining intervals are outside the measured handler stages.
Socket samples every 250 ms and process-relative clocks cannot resolve the
relative contribution of client wake-ups, socket queues, backend work or
descheduling to these requests. The Colima replay did not reproduce the
prior 117.85 ms p99.9 breach. A native reproduction of the tail and the
original transport errors remains open.

The socket wrapper observes application I/O boundaries. A flush does not prove
peer receipt. HTTP/2 multiplexes frames across streams. Connection-level I/O
therefore lacks exact per-stream wire attribution. Body-end and downstream
handoff mark the same streaming-body boundary, followed by separately recorded
connection flush. In the archived replay, “cancelled” upload handoff marks the
unused HTTP/1 empty-body wrapper dropping. The final candidate labels known-empty
uploads explicitly. Absolute deadlines and retry settings were preserved.
[Correlated replay and TCP samples](../bench/program/evidence/milestone-2c/incident-raw.tar.gz)
[Replay summary](../bench/program/evidence/milestone-2c/incident-summary.json)
[Historical incident](PERFORMANCE-MILESTONE-2B.md#two-worker-transport-incident).

## Endpoint-sharded pool experiment

Use `CAUCE_POOL_STRATEGY=sharded` at process startup to test the real transport
implementation. Endpoint mutexes isolate normal acquisition and return.
Owned credits preserve global and endpoint bounds for physical and idle
connections. HTTP/1.1 has one opening per endpoint at a time in this strategy.
HTTP/2 keeps the existing stream permits and serialized opening.
A rotating scan reclaims idle sockets under global pressure. Generation
identity, readiness, cancellation, TLS isolation and connection draining
are checked separately from pool layout.
[Decision and remaining risks](adr/0008-endpoint-sharded-pool.md).

The Linux ARM64 guest matrix used two Tokio workers, 128 requests/s,
both protocols, and 1, 4, 16, 64, 256 and 1,024 logical endpoints. It
ran a round-robin phase and a second phase directed wholly at one endpoint.
Distinct logical endpoints have independent keys while sharing a loopback
backend address. The test covers actual socket churn when the endpoint count
exceeds the 32-connection budget. All 24 cells accounted for 13,312
successful requests and unique complete JSONL events. Supplemental
one- and four-worker runs used the final runtime at 1, 16 and 64 endpoints.
They accounted for all 1,536 requests in 24 more cells. In some churn cells
the backend briefly observed 33 sockets while the proxy owned a
32-connection permit budget. The backend close event and the proxy's
permit return are distinct boundaries.

| Protocol and endpoint count | Compact wait/hold p99 | Sharded wait/hold p99 |
| --- | ---: | ---: |
| HTTP/1.1 · 1 | 0.17 / 4.08 µs | 0.29 / 4.21 µs |
| HTTP/1.1 · 64 | 0.17 / 8.71 µs | 0.54 / 5.63 µs |
| HTTP/1.1 · 1,024 | 0.17 / 8.50 µs | 0.58 / 5.75 µs |
| HTTP/2 · 1 | 0.25 / 3.96 µs | 0.29 / 3.75 µs |
| HTTP/2 · 64 | 0.13 / 3.46 µs | 0.38 / 5.08 µs |
| HTTP/2 · 1,024 | 0.17 / 2.25 µs | 0.46 / 3.17 µs |

These measurements include diagnostic clock reads and VM scheduling effects.
The evidence is insufficient to set an adaptive threshold. Configuration-time
auto selection, saturated-stream indexes and the hybrid thread-local model
remain outside the retained strategy. Qualified native tests should include
multiple backend addresses and failure isolation, high concurrency, fairness,
memory, churn, and the one-endpoint control.
[Full matrix](../bench/program/evidence/milestone-2c/pool-matrix.json)
[Socket and lock evidence](../bench/program/evidence/milestone-2c/pool-matrix-raw.tar.gz)
[Supplemental 1- and 4-worker evidence](../bench/program/evidence/milestone-2c/pool-matrix-workers-1-and-4.tar.gz).

## Native capacity and competitive matrix

| Host | Capacity and worker scaling | Hardware CPU and allocation profiles |
| --- | --- | --- |
| Linux ARM64 Graviton | Blocked pending host authorization | Blocked. Guest PMU exposure unverified |
| Linux AMD64 EPYC | Blocked pending host authorization | Blocked. Guest PMU exposure unverified |
| Four-vCPU Linux ARM64 Colima | Correctness and diagnostic evidence only | Whole-process allocation diagnostics available |

The original estimate is $8.07 compute and storage, or $8.11 with ephemeral
IPv4. A 16-vCPU account quota prevents that Intel selection. The revised
sequential Graviton/AMD estimate is $5.68 before variable transfer and
taxes. The launch tooling records host topology, per-event PMU probes,
and checksummed raw evidence, and includes cleanup. Setup and teardown
await live authorization. No lab resources were created.
[Lab setup and cleanup](NATIVE-LINUX-BENCHMARKS.md)
[Sanitized preflight and pricing rows](../bench/program/evidence/milestone-2c/aws-preflight.json).

| Workload | Cauce evidence | Comparison contract and availability |
| --- | --- | --- |
| HTTP/1.1 cleartext, logs off | The native runner supports candidates from `ba6f952`, `f754e44`, `54b2529`, `2619351` and 2C | NGINX OSS 1.30.8 has historical VM characterization. HAProxy 3.4.8 and Pingora 0.9.1 have bodyless GET fixture checks. Native paired and capacity comparisons are open. |
| HTTP/1.1 cleartext, logs on | 2C allocation diagnostics and recovery validation | The existing NGINX fields, buffering and durability differ from Cauce JSONL. Equivalent competitor log fixtures remain open. |
| HTTP/1.1 multiple upstreams | Compact and sharded socket matrix | Native paired measurements and multi-upstream competitor fixtures are open. |
| HTTP/2 cleartext and TLS | Linux protocol, lifecycle, stress and deadline suite | Pinned-version equivalent fixtures remain open. The existing Pingora fixture uses HTTP/1.1 upstream. |
| gRPC unary and bidirectional | Linux suite exercises all four streaming shapes | Pinned-version equivalent fixtures remain open. HAProxy and Pingora cleartext smoke fixtures do not cover them. |
| TLS handshake, reuse, SSE and WebSocket | Real-socket TLS and tunnel gates and streaming checks | Competitive cells remain open. |
| Large bodies and slow clients | Flow control, request/response limits and cancellation checks | Competitive cells remain open. Pingora bodyless GET smoke excludes uploads. |
| Overload and recovery | Bounded pool and transport recovery checks | HAProxy queues at the server connection limit. Pingora immediately rejects at its request semaphore. Report them as distinct workload contracts until matched. |

Run adaptive ramps with direct backend controls at twice the target rate.
Confirm every candidate at the existing p99 ≤ 20 ms, p99.9 ≤ 100 ms,
99% offer-window goodput and exact zero-error accounting gates for at least
60 seconds. Follow with a stability hold and compare 1, 2, 4 and 8 workers.
A Colima smoke with four logical endpoints confirmed 25/s for three seconds
and produced valid output checksums. It is a harness verification.
Use `native-linux.py` to generate the worker and capacity curves from raw
cells. Separate control failures from proxy limits. Use pinned fixture
configurations and test actual behavior before collecting competitive timings.
[Benchmark methodology](BENCHMARK-METHODOLOGY.md)
[Reference contracts](../bench/program/references/README.md)
[Runner smoke](../bench/program/evidence/milestone-2c/native-runner-smoke.tar.gz).

Use `telemetry.py` for randomized paired healthy-load measurements with
independent physical cores and the Rust generator. For sharding, use the
same binary for both variants and select `--before-strategy compact` and
`--after-strategy sharded`. Start with 1 and 64 endpoints in separate cells.
With `--require-http1-slo` the paired runner checks the latency and goodput
gates alongside zero-error and account checks. Predeclare a practical
improvement of at least 5% in CPU per success at a matched rate, and require
the paired 95% interval to support that improvement. Inspect tail latency,
memory, reuse and every failure separately. Collect at least seven valid
pairs per cell and independently audit the offers, backend counts, metrics and
raw access events. Confirm that hardware and scheduler counters actually ran
before reporting their values. Profile in separate cells.

The 2B sampled hotspots remain the starting point for native CPU attribution.
2C addresses header decoding and JSON serialization directly. HPACK,
Notify polling, per-stream deadlines and connection-driver wake-ups need
native samples before further changes. The parity and superiority gates
remain open.

## Verification

| Gate | Result |
| --- | --- |
| Rust format, strict Clippy and all targets | 48 tests passed with one route microbenchmark ignored on macOS ARM64 and Linux ARM64. |
| Real sockets, YAML/TOML, reload and rollback | 23 cases passed with compact and 23 with sharded. YAML and TOML examples passed validation. |
| Cancellation and ownership | Real HTTP/1.1 and HTTP/2 eviction and opening-cancellation regressions passed. Two Loom models passed. |
| TLS, WebSocket, HTTP/2 and gRPC | Linux TLS lifecycle, WebSocket edges, HTTP/2 and gRPC integration, stream stress, and absolute HEADERS deadlines passed. |
| Collector and Linux resilience | 23 collector cases, 13 storage cases, and all three Linux resilience suites passed. |
| AddressSanitizer | The Linux HTTP/2 header observer completed 100,000 runs with seed 60616 and no crash artifact. |
| Benchmark and infrastructure tools | Ten Python harness tests passed. The provisioner passed local mocks for SSH evidence retrieval and teardown on success and remote failure. The EC2 code awaits live validation. |

[Raw regression evidence](../bench/program/evidence/milestone-2c/regressions-raw.tar.gz)
[ASan log](../bench/program/evidence/milestone-2c/asan-header-observer.log.gz)
[Checksums](../bench/program/evidence/milestone-2c/SHA256SUMS).
