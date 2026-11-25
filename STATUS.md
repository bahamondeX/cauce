# Cauce engineering program

## Baseline

The examined checkout is Cauce 0.9.1 at `ba6f95277f5ffca966faea5e25203c1b75d310d5`. Its Cargo.lock SHA-256 is `92b4450e7d190df4b71549a462e4d5fee0800b701a22ad8fdd65f68570873316`. It pins 189 package records, including Tokio 1.54.1, Hyper 1.12.0, hyper-util 0.1.21, h2 0.4.20, rustls 0.23.46, ring 0.17.14, reqwest 0.12.28, rusqlite 0.37.0 and TOML 0.8.23.

NGINX OSS 1.30.8 is the program reference. The [compatibility matrix](compatibility/nginx-oss.yaml) enumerates 120 modules, 754 directive registrations and 149 configure option patterns from the checksummed source tarball, including conditional and platform-specific definitions. Pinned njs 1.0.2 and nginx-acme 0.4.2 contribute five add-on modules and 70 additional directive registrations. Directive and behavior parity remain in progress.

## Implemented on this branch

- Configuration generations compile the host and path routing index. Request handling retains exact authorities, path boundaries, longest-prefix selection and existing protocol checks. [ADR 0001](docs/adr/0001-compiled-routes.md).
- SIGHUP preparation and pool sweeps run off the accept loop. Accepted configurations publish as a complete generation. Invalid candidates preserve the current generation. [ADR 0002](docs/adr/0002-prepared-reload.md).
- The proxy reads versioned YAML alongside TOML. It supports unit-suffixed durations, scalar environment references and redacted validation output. `cauce check` validates both formats. The YAML example includes a schema modeline. [Configuration guide](docs/CONFIGURATION.md) [JSON Schema](schemas/cauce.schema.json).
- The offline NGINX migration command reports unsupported directives and semantic differences and can generate a review-only YAML preview for a narrow cleartext HTTP configuration. [Migration instructions](docs/CONFIGURATION.md#nginx-migration-report).
- Access events and per-attempt diagnostics are constructed only when the JSONL logger is enabled. Request identity, trace forwarding, metrics, and HTTP/1 and HTTP/2 lease handling are exercised separately from event construction. [ADR 0003](docs/adr/0003-optional-access-events.md).
- Collector recovery distinguishes a previously scanned incomplete tail from unread complete records. The checkpoint migration preserves existing records and permits a repaired, rotated inode to shrink precisely to the committed offset. [Collector contract](docs/COLLECTOR.md).
- The HTTP/1 benchmark runner and independent auditor accept ARM64 and AMD64 Linux Docker engines and verify the architecture of the proxy and fixture images against the engine. The runner records `nginx -V` and stops on version-probe failure.

The candidate lockfile adds yaml_serde 0.10.7 and libyaml-rs 0.3.0. Existing locked versions were preserved. Candidate Cargo.lock SHA-256 is `7074e024b13f9e26c0755ea3a90435533759a48d0145afd35890ab2f374629d4`.

## Verification on macOS ARM64

| Check | Outcome |
| --- | --- |
| Baseline `cargo test --locked --all-targets` | 39 passed. An initial disk-full build and a rerun using a coarse-timestamp RAM filesystem exposed environmental failures. The completed run used APFS for test fixtures and the RAM volume for compiler artifacts. |
| Candidate Rust unit tests | 44 passed with the collector recovery case. The lookup measurement runs separately. [Final checks](bench/program/evidence/final-check-darwin-arm64.log). |
| Candidate `cargo fmt --check` and `cargo clippy --locked --all-targets -- -D warnings` | Passed. |
| Candidate `tests/integration.py` | 22 passed over real sockets, including TOML reload, YAML reload and rollback, fresh clients during a stalled candidate, and request identity and metric accounting with JSONL disabled. [Log](bench/program/evidence/telemetry-integration-darwin-arm64.log). |
| Collector integration and storage suites | 23 and 13 passed using local fixtures. |
| Benchmark harness tests | Increment 7 Python 10 passed. Increment 8 Python 5 passed and Node 6 passed. |
| Versioned YAML and NGINX preview | YAML and TOML examples passed `cauce check`. Draft 2020-12 schema and YAML example validated. Migration preview passed `cauce check`. The negative case reported include and stream directives and produced no candidate. |
| Route lookup benchmark | At 16–1,024 routes the candidate reduced lookup time for the tested hit workloads. The single-route case took roughly 1 ns longer. Measures lookup cost only. |
| Access-event A/B benchmark | The revised run completed all 72,000 scheduled offers. The generator exceeded the jitter budget in 19 of 20 cells. The campaign yielded zero valid before/after pairs. [Raw data](bench/program/evidence/telemetry-darwin-arm64-retry.tar.gz). |

[Verification logs and raw lookup samples](bench/program/evidence/) [Performance method and regression report](docs/PERFORMANCE.md).

## Linux verification

The local Linux lab is a four-vCPU AArch64 Colima guest on Apple ARM64. Testing there can establish Linux ARM64 behavior and VM-scoped performance. Native production-architecture results require separately identified Linux ARM64 and AMD64 hardware.

| Check | Outcome |
| --- | --- |
| Baseline image rebuild from `git archive HEAD` | Completed with build cache hits. Image `sha256:c3d2624e0d74475762334cfa25241b3900c524a08310955db72192873cdc03db`. |
| Baseline Docker resilience | Proxy failure and log-disk suites passed after recovery. The collector-storage suite reproduced a pre-existing failure during repair and rotation of a checkpointed log with a scanned incomplete tail. [Recovered baseline report](bench/program/evidence/baseline-resilience-recovered-linux-arm64.tar.gz). |
| Candidate Docker resilience | All three suites passed with the rebuilt Linux ARM64 image. The one-event batch case proved exact event IDs and SQLite integrity after the logger handed off the repaired inode. Real tmpfs ENOSPC and collector SIGKILL cases passed. [Results and source hashes](bench/program/evidence/candidate-resilience-linux-arm64.tar.gz). |
| Candidate HTTP/2 and gRPC integration | Nine protocol check groups and two active-request lifecycle cases passed in the Linux ARM64 guest. The proxy binary is shared with the resilience candidate. [Protocol and lifecycle report](bench/program/evidence/candidate-http2-linux-arm64.json). |
| Baseline Linux `cargo test --locked --all-targets` | Interrupted during dependency compilation by guest I/O errors and a read-only filesystem. |
| Address-sanitized HTTP/2 header observer | Colima restart recovered the daemon. A fresh image ran 100,000 executions with seed 60616. The run produced no crash artifacts. The target and production observer match `ba6f952` byte-for-byte. [Raw log](bench/program/evidence/fuzz-recovered-linux-arm64.log.gz). |
| NGINX 1.30.8 Alpine benchmark replay | The isolated 250 requests/s retry completed all 12 cells and passed the independent audit of 24,000 successful requests. Earlier campaigns with generator losses are retained. [Accepted run](bench/program/evidence/nginx-alpine-arm64-isolated-raw.tar.gz) [500 requests/s rejection](bench/program/evidence/nginx-alpine-arm64-generator-rejection.tar.gz) [250 requests/s rejection](bench/program/evidence/nginx-alpine-arm64-250-generator-rejection.tar.gz). |

The historical HTTP/1 cleartext comparisons covered Cauce 0.7 and NGINX 1.30.6
at 500 requests/s and published Cauce 0.9.1 and NGINX 1.30.8 Alpine at 250
requests/s. The routing datasets support lookup claims for the recorded
workloads on macOS ARM64 and the Linux ARM64 guest.

The normal Dockerfile build stalled while resolving pinned Debian metadata.
The Linux integration runs used a local test image assembled from the baseline
runtime image and binaries compiled offline with the pinned Rust image.
[Proxy and collector build](bench/program/evidence/candidate-offline-linux-arm64.log)
[Collector rebuild](bench/program/evidence/collector-rebuilt-linux-arm64.log).

## Milestone 2

Checkpoint `f754e44` preserves the Milestone 1 tree. Default Dockerfile builds
work again. A package cache fix prevents an older source snapshot from reusing
another revision's executable. Both Cauce references and the HTTP/2 experiment
used the repaired release build. [Build decision](docs/adr/0004-docker-release-cache.md).

The native open-loop generator passed the 10 ms scheduler p99 gate in all 18
three-way HTTP/1.1 cells. Published Cauce, the checkpoint and NGINX all completed
250 successful requests/s in the Colima guest. NGINX used less CPU per
successful request in the three-repetition characterization. The separate
eight-pair logs-off telemetry comparison left the CPU effect inconclusive.
[Milestone 2 report](docs/PERFORMANCE-MILESTONE-2.md)
[Benchmark methodology](docs/BENCHMARK-METHODOLOGY.md).

The HTTP/2 candidate ends known-empty uploads in initial HEADERS. Cleartext,
TLS, zero-data trailers, gRPC lifecycle, stress and Linux resilience gates
passed. The fixed-rate HTTP/2 campaign verified the framing reduction and
left CPU and latency effects inconclusive. One Tokio worker confirmed 399.7
successful requests/s in the cleartext capacity fixture. Two-worker runs
failed the campaign SLO. The guest profiles cover HTTP/1.1, HTTP/2 cleartext,
gRPC unary and bidirectional streaming. Native Linux results, multicore
saturation and equivalent competitor capacity remain open.

## Milestone 2B

The branch fixes a reproducible HTTP/1.1 sender readiness race and missed
pool wake-ups after canceling an opening reservation. It removes temporary
pool key copies, counts idle HTTP/1.1 sockets without a return-path scan,
and reuses buffers in the JSONL writer. The original two-worker transport
failure remains unresolved after a correlated diagnostic replay.

The Linux runner now records adaptive ramps, controls, confirmations,
resource samples and scaling data. Production-wide capacity and parity gates
remain open pending qualified ARM64 and AMD64 campaigns. The program stays
in dataplane performance and concurrency optimization.
[Milestone 2B evidence and open gates](docs/PERFORMANCE-MILESTONE-2B.md).
