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

## Milestone 2C

Header preprocessing and typed JSONL fields removed request-path allocations.
In the Colima diagnostic replay, the compact 2C candidate used 34.77 whole-process
allocations per success compared with 53.05 for 2B. All three Heaptrack inputs
produced exactly 2,100 complete access events. Peak heap and build differences
are recorded with the raw data.

Endpoint sharding is available through `CAUCE_POOL_STRATEGY=sharded`.
Compact remains the default. Both strategies served all 13,312 offers in
the Linux ARM64 transport matrix at up to 1,024 logical endpoints. The
two-worker trace completed 9,200 requests without a transport failure.
The historic pre-headers incidents and tail breach remain unresolved.

Milestone 2C completed AWS authentication and read-only inventory.
Native capacity, profiling and comparative work carried into Milestone 3.
[Milestone 2C report](docs/PERFORMANCE-MILESTONE-2C.md)
[Native lab](docs/NATIVE-LINUX-BENCHMARKS.md)
[NGINX compatibility backlog](docs/NGINX-COMPATIBILITY-BACKLOG.md).

## Milestone 3 checkpoint

- **Native performance.** The AMD64 lab independently audited 8,487,500
  measured offers across the primary and supplemental campaigns. At
  1,000 offers/s with one worker and logs disabled, `47da8fe` used
  40 µs CPU per successful HTTP/1.1 request versus 46 µs for NGINX
  1.30.8 across seven qualified pairs. Cauce's p99 was higher.
  The 60-second confirmations reached 7,500/s for one-worker Cauce
  and at least 8,000/s for one-worker NGINX. Generator and direct
  control limits left Cauce scaling efficiency open.
  [Native evidence and coverage](docs/PERFORMANCE-NATIVE.md).
- **Functional parity.** Wildcard virtual hosts and literal exact
  locations passed 11 shared NGINX real-socket cases. The source
  registry records the remaining host, path, and normalization
  differences. [Functional parity](docs/FUNCTIONAL-PARITY.md).
- **Production readiness.** YAML and TOML accept checked binary byte
  units. The migration preview handles literal host and route
  aliases, reports confidence and supplies a validation command.
  The experimental sharded pool preserves a requester's reusable
  sockets during global eviction. Compact remains the default.
  [YAML and migration](docs/CONFIGURATION.md)
  [Pool decision](docs/adr/0008-endpoint-sharded-pool.md).
- **Validation and infrastructure.** Rust, real-socket, collector,
  Linux resilience, HTTP/2 and gRPC, and 100,000 AddressSanitizer
  fuzz executions passed. Two Graviton startup attempts were
  terminated and independently verified. AMD64 accepted guest
  poweroff. AWS reauthentication is required to verify its
  control-plane cleanup and start the staged ARM64 campaign.
  [Evidence, cost and cleanup](docs/PERFORMANCE-NATIVE.md#aws-controls).

## Milestone 4 checkpoint

- **Gateway.** Flat location precedence and compiled route-local header
  policies passed 20 shared NGINX 1.30.8 cases. YAML and TOML validation,
  reload and rollback cover the new options. [Functional parity](docs/FUNCTIONAL-PARITY.md).
- **Dataplane.** Encoded-path validation removes one allocation per
  call in both tested workloads and preserves the prior rejection rules.
  [Performance evidence](docs/PERFORMANCE-MILESTONE-4.md).
- **Capacity.** An audit of 8,487,500 measured offers preserved
  Cauce's 7,500/s confirmation and reclassified it as a lower bound.
  The 8,000/s ramp failed in warm-up. Multicore efficiency and a
  comparable saturation boundary remain open.
- **AWS.** The ARM64 suite is staged. The SDK reported
  `LoginRefreshRequired` for the authorized profile. AMD64 cleanup
  and the prior invoice still need control-plane verification, so
  this checkpoint launched no new resources. [Lifecycle evidence](docs/PERFORMANCE-MILESTONE-4.md#aws-lifecycle).

## Milestone 5 checkpoint

- Trusted IPv4/IPv6 client identity, bounded rewrites and redirects, private
  named locations, per-route balancing groups, shared rate zones and HTTP/TCP
  admission are configurable in YAML and TOML. Compact pooling remains the
  default. [Production YAML](examples/production-gateway.yaml)
  [Policy ordering](docs/adr/0011-compiled-gateway-policies.md).
- Both pool strategies passed the pinned NGINX gateway socket comparison and
  existing proxy regression. The registry records 10 verified observable
  behaviors, 13 partial NGINX modules and 51 partial directive registrations.
  [Compatibility report](docs/FUNCTIONAL-PARITY.md#milestone-5-production-gateway).
- The 8,000/s warm-up lost 237 offers after the generator dispatch pipeline filled.
  The cause of the 41.663 ms completion gap remains unattributed. AWS
  reauthentication blocks prior lifecycle reconciliation and fresh native
  performance qualification. [Milestone 5 evidence](docs/PERFORMANCE-MILESTONE-5.md).
- The independent health and TLS additions passed their socket tests with the
  standard gRPC health service and real TLS peers. They add active HTTP/2 and
  gRPC checks, exact SNI and a default certificate, listener-wide mTLS,
  upstream client certificates and configurable versions and resumption.
  [Health YAML](examples/http2-health.yaml)
  [Mutual TLS YAML](examples/gateway-mtls.yaml).

## Milestone 6 checkpoint

- Typed routes serve static content and direct responses alongside the
  reverse proxy. Static delivery confines file opens to a document directory
  and supports conditional GET, HEAD and bounded byte ranges.
  [Static file guide](docs/STATIC-FILES.md).
- Public proxy routes can enable a bounded memory cache with per-key
  request collapse, revalidation and stale policies. Gzip, Brotli and
  precompressed static files are opt-in. [Cache guide](docs/HTTP-CACHE.md)
  [Compression guide](docs/COMPRESSION.md).
- STS confirmed the authorized account and verification found the prior
  benchmark instance, volumes and interfaces absent. The remaining lab
  security group, role and profile were removed and verified absent.
  Billing attribution still blocks additional AWS provisioning.
  [Milestone 6 evidence](docs/PERFORMANCE-MILESTONE-6.md).

## Milestone 7 checkpoint

- **Layer 4.** Independent TCP and UDP listeners, TLS passthrough with
  bounded SNI and ALPN preread, and trusted PROXY v1/v2 are implemented.
  PostgreSQL, Redis, OpenSSH and DNS fixtures exercise real wire traffic.
  [TCP](docs/TCP-PROXY.md) [UDP](docs/UDP-PROXY.md)
  [TLS passthrough](docs/TLS-PASSTHROUGH.md).
- **HTTP/3.** Quinn and h3 serve requests through Cauce's existing HTTP
  policies. Independent HTTP, HTTPS and QUIC sockets support TLS
  certificate reload on established bindings. [HTTP/3](docs/HTTP3.md).
- **Compatibility and performance.** The source registry records the
  verified NGINX Stream and HTTP/3 cases. Linux ARM64 and AMD64 artifacts
  are available locally. The milestone report records regression gates,
  benchmark qualifications and remaining DNS, resilience, cache and ACME
  work. [Milestone report](docs/PERFORMANCE-MILESTONE-7.md).

## Milestone 8 functional checkpoint

- **Application gateways.** FastCGI, native uWSGI and SCGI share the compiled HTTP route and response pipeline. The adapters have independent framing and exclusive socket leases. FastCGI supports bounded sequential reuse. PHP-FPM and both native WSGI wire modes passed matching cases against NGINX 1.30.8. [FastCGI](docs/FASTCGI.md) [uWSGI](docs/UWSGI.md) [SCGI](docs/SCGI.md).
- **Mail.** Independent SMTP submission, IMAP and POP3 listeners use TLS, authorize through an `auth_http` subset, authenticate to an allowlisted backend and relay with bounded backpressure. Postfix and Dovecot fixtures passed with Cauce and NGINX. Mail topology and certificates require restart. [Mail proxy](docs/MAIL-PROXY.md).
- **Release qualification.** The checkpoints are `ed8f4b8`, `6b4bb31` and `710188d`. The evidence records test coverage, binary growth, disabled-protocol diagnostics and requirements for native qualification. Cauce 1.0 still requires the independent infrastructure increments and production qualification. [Milestone 8](docs/PERFORMANCE-MILESTONE-8.md) [Roadmap](docs/ROADMAP.md).

## Milestone 9A DNS checkpoint

Optional shared recursive DNS now follows address TTLs across HTTP,
application gateways, active HTTP health checks, Stream and mail auth HTTP.
Both pool strategies retire removed IPs while existing sessions finish.
Mail retains its literal authorization allowlist and can dial a configured
named backend with verified TLS.
[DNS reference and evidence](docs/DNS-AND-RESILIENCE.md).

## Milestone 9 infrastructure candidate

- HTTP and application endpoints support passive ejection, jittered
  backoff, single half-open recovery and opt-in bounded pre-send failover.
  [DNS and resilience](docs/DNS-AND-RESILIENCE.md).
- HTTP cache adds detached revalidation, invalidation fences and a bounded
  disk tier. [HTTP cache](docs/HTTP-CACHE.md) [Disk cache](docs/DISK-CACHE.md).
- Stream TCP supports verified TLS on either side. ACME can rotate
  shared HTTPS and QUIC certificates using HTTP-01. [Stream TLS](docs/TCP-PROXY.md#stream-tls)
  [ACME](docs/ACME.md).
- Six Cargo profiles and a redacted CLI summary support distribution
  tests. The source-derived backlog tracks OSS and add-ons separately.
  [Profiles](docs/BUILD-PROFILES.md) [1.0 gaps](docs/CAUCE-1.0-GAP-ANALYSIS.md).

- Stream and mail socket additions and removals prepare bindings before
  publication. Established TCP and authenticated mail sessions drain,
  and retired UDP listeners preserve prior peers under their deadlines.
  [Lifecycle policy](docs/LISTENER-LIFECYCLE.md).

The Linux ARM64 fixture passed 20 routing cases, 11 gateway cases per
pool layout and 15 content cases against pinned NGINX OSS 1.30.8. The
source audit records 134 of 754 directive registrations as partial and
620 as unmapped. Current native throughput and multicore qualification,
retained-socket TLS reload and the bundled SQLite advisory remain release
gates. [Qualification evidence](docs/PERFORMANCE-MILESTONE-9.md).

## Milestone 10 compatibility checkpoint

Cauce now compiles scoped nested locations and inherits selected policies
at load time. Literal response header additions and explicit origin SNI
settings have pinned NGINX differential evidence. The registry contains
137 partial OSS registrations, two mapped gaps and 615 unmapped registrations.
The runtime's patched collector links SQLite 3.54.1.
[Milestone evidence](docs/PERFORMANCE-MILESTONE-10.md)
[Compatibility tooling](docs/COMPATIBILITY-TOOLING.md)
[SQLite remediation](docs/SQLITE-SECURITY.md).
