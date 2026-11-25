# Performance evidence

## Route matching, Darwin ARM64

The 0.9.1 request handler scanned every configured route, then cloned the winning route. At the 1,024-route configuration limit, it ran up to 1,024 host comparisons per request and up to 1,024 prefix checks when one authority owned the routes. Pool inspection found one mutex protecting both HTTP/1 and HTTP/2 indexes, including scans during reuse and periodic sweep. Measure contention under multi-worker, multi-endpoint load before changing the pool design.

The routing hypothesis was that a host lookup followed by a compressed path trie would bound lookup cost by request key length, while preserving exact authority and longest segment-aware prefix. The candidate keeps a direct comparison for a one-route configuration. The benchmark uses the published matcher with its winning route borrowed, which isolates selection cost and excludes the cost of the old clone.

- Host and compiler are recorded in the [environment file](../bench/program/evidence/routing-darwin-arm64-environment.txt).
- Each row below is a median of seven 262,144-lookup runs from the [raw samples](../bench/program/evidence/routing-darwin-arm64.csv). Each case warms both matchers and alternates order. Hosts and paths are preallocated, all requests hit, and configurations have either one host or one host per route. The [summary CSV](../bench/program/evidence/routing-darwin-arm64-summary.csv) includes paired bootstrap intervals over the seven repetitions.
- Run `bash bench/program/routing.sh /tmp/routing.csv` and `python3 bench/program/summarize-routing.py /tmp/routing.csv`. The harness compiles `src/routing.rs` directly with a minimal fixture for the configuration types. Cargo runs the same routing regression against the shipped type.
- A separate invocation of the published script reproduced the multi-route gains and the roughly 1 ns single-route cost. [Reproduction samples](../bench/program/evidence/routing-darwin-arm64-reproduction.csv) [Reproduction summary](../bench/program/evidence/routing-darwin-arm64-reproduction-summary.csv).
- This checkout reproduced the pattern at 16–1,024 routes. [Confirmation raw samples](../bench/program/evidence/routing-darwin-arm64-confirmation.csv) [Environment](../bench/program/evidence/routing-darwin-arm64-confirmation-environment.txt) [Summary](../bench/program/evidence/routing-darwin-arm64-confirmation-summary.csv).

| Layout | Routes | Linear median ns | Indexed median ns | Median paired ratio |
| --- | ---: | ---: | ---: | ---: |
| One host | 1 | 9.95 | 10.89 | 0.915× |
| One host | 16 | 131.37 | 44.29 | 2.981× |
| One host | 128 | 934.24 | 50.52 | 17.549× |
| One host | 1,024 | 7,058.09 | 87.50 | 79.950× |
| Distinct hosts | 1 | 10.29 | 11.13 | 0.923× |
| Distinct hosts | 16 | 36.95 | 25.85 | 1.432× |
| Distinct hosts | 128 | 201.25 | 24.92 | 8.065× |
| Distinct hosts | 1,024 | 1,832.21 | 36.21 | 53.141× |

One-route lookup took roughly 1 ns longer in the harness. Larger configurations improved on this lookup workload. The candidate also removes the route clone and avoids host normalization allocation for lowercase authorities. Keep those code changes together pending a release-build HTTP regression run with both one-route and large-route cases. These measurements do not include parse, network, allocation, TLS, contention, or client goodput.

## Linux ARM64 VM measurements

The same lookup workload ran on the Colima Linux AArch64 guest with one pinned vCPU and Rust 1.88. At 16–1,024 routes, paired median lookup ratios ranged from 1.165× (16 distinct hosts) to 54.389× (1,024 distinct hosts). The Linux one-route fast path took about 1 ns longer. This is a virtualized, same-ISA result. [Raw samples](../bench/program/evidence/routing-linux-arm64-colima.csv) [Summary and intervals](../bench/program/evidence/routing-linux-arm64-colima-summary.csv) [Environment](../bench/program/evidence/routing-linux-arm64-colima-environment.txt).

The historical HTTP/1.1 replay used Cauce 0.7, NGINX 1.30.6, the normal cleartext scenario, three repetitions per stack/logging cell, two seconds of warm-up and eight seconds at 500 offered requests/s. The independent auditor validated all 12 runs, the capacity gates and 48,000 successful requests. The measured stacks each delivered approximately 500 useful requests/s. With logs disabled, past Cauce and NGINX had median p95 of 2.90 and 2.91 ms and median proxy CPU of 7.50% and 6.02% of one core, respectively. [Historical raw data, manifests and audited summaries](../bench/program/evidence/linux-arm64-http1-historical-and-initial-attempt.tar.gz).

The updated auditor derives p99.9 directly from successful per-offer rows and
CPU microseconds per success from the proxy cgroup counters. The historical
logs-off medians are 9.77 and 10.57 ms p99.9 and 151.72 and 123.45 µs CPU per
success, in the same Cauce 0.7 and NGINX 1.30.6 order. [Re-audited historical summary](../bench/program/evidence/http1-historical-re-audit.json).

The first NGINX 1.30.8 Debian image attempt had a mistyped digest. The corrected digest resolved to the expected ARM64 image, whose locally stored executables returned an exec format error after the filesystem incident. Two Cauce 0.9.1 cells completed, and the partial dataset is preserved. The 1.30.8 Alpine package passed the executable/version gate. Its root filesystem, compiler and build arguments differ from the Debian package. [Failed and partial raw data](../bench/program/evidence/linux-arm64-http1-baseline-retry.tar.gz) [Debian image diagnostics](../bench/program/evidence/nginx-linux-arm64-image-diagnostics.log) [Alpine build identity](../bench/program/evidence/nginx-linux-arm64-alpine-pull.log).

Attempts at 500 and 250 offered requests/s with the Alpine image stopped when the load generator dropped or rejected offers. The archived runs preserve per-offer rows, gate records and environment details. [500 requests/s](../bench/program/evidence/nginx-alpine-arm64-generator-rejection.tar.gz) [250 requests/s](../bench/program/evidence/nginx-alpine-arm64-250-generator-rejection.tar.gz).

The isolated retry compared published Cauce 0.9.1 at `ba6f952` with NGINX 1.30.8
Alpine at digest `8eb04fe3b385090e9aa02961f77398ad09b6bf33ab07f6669a5bb7acce22c0d3`.
It used cleartext HTTP/1.1, a 1 KiB response, one proxy worker, three repetitions
per stack and logging variant, two seconds of warm-up, and eight seconds at 250
scheduled requests/s. All 12 capacity gates and 24,000 measured requests passed
the auditor. Both stacks delivered about 250 successful requests/s.

| Logs | Proxy | p50 ms | p95 ms | p99 ms | p99.9 ms | CPU µs per success | RSS MiB | Cgroup memory MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Off | Cauce | 2.05 | 3.30 | 5.81 | 12.31 | 192.38 | 5.88 | 2.63 |
| Off | NGINX | 1.73 | 3.44 | 11.72 | 43.65 | 153.45 | 8.94 | 3.04 |
| On | Cauce | 2.11 | 3.66 | 5.68 | 12.87 | 233.75 | 7.88 | 6.28 |
| On | NGINX | 1.89 | 3.03 | 4.09 | 6.16 | 200.47 | 9.82 | 3.73 |

Each value is the median of three per-run values. The memory columns use observed
per-run peaks. Cauce used more CPU per successful request in both logging modes.
Tail latency favored different proxies by mode. The runs qualify a fixed-rate
baseline on the Colima Linux ARM64 guest. The release gates still require native
hardware, rate sweeps, and additional workloads.
[Machine-readable summary](../bench/program/evidence/nginx-alpine-arm64-isolated-summary.json)
[Raw data and image identities](../bench/program/evidence/nginx-alpine-arm64-isolated-raw.tar.gz).

## Linux and reference comparisons

Use the existing open-loop [HTTP/1 harness](../bench/increment7/README.md) and [HTTP/2 and gRPC harness](../bench/increment8/README.md) for historical reproductions. They pin NGINX 1.30.6 for their original configurations. For a new campaign, pass `--nginx-image nginx:1.30.8@sha256:755983e0e0e568e4c86297cc91485d7a6f9fb73e42d8a19cdba3eac9e8b8cfc9` and record the platform-specific image ID and `nginx -V`. The compatibility program pins the [NGINX OSS source release](https://nginx.org/en/download.html).

Run on separate native Linux ARM64 and AMD64 machines with the platform, kernel, CPU, topology, affinity, cgroup limits and compiler recorded. Keep client, backend and proxy on dedicated CPU sets. Validate body, trailers, error classification, TLS and capacity behavior before timing each stack. Discard a run from comparative claims if demand is missed, the scheduler lags, counts disagree, or CPU throttling changes the budget. Retain the raw run and the rejection reason.

Use the Milestone 2 load-rate runner for scheduled-arrival latency and
offer-window goodput under workload-specific SLOs. Record proxy CPU, reuse,
handshakes, Linux PSS/cgroup memory and available kernel counters. Run cold
start, reload, overload and recovery campaigns separately from steady state.
HAProxy and Pingora comparisons require equivalent resource and protocol gates.
[Methodology](BENCHMARK-METHODOLOGY.md).

The selected source pins for those future comparisons are HAProxy 3.4.8 (SHA-256 `024bd521a6cbbe32b426853147e6bbd6a89b107c5a34144846193fee5b7c6ed1`) and Pingora 0.9.1 (commit `f86ffda5d3ae5c3baf5125682138b33b26c01b08`). Lock the built binary, settings and cryptographic provider together when recording a run. [HAProxy source checksum](https://www.haproxy.org/download/3.4/src/haproxy-3.4.8.tar.gz.sha256) [Pingora source](https://github.com/cloudflare/pingora/tree/f86ffda5d3ae5c3baf5125682138b33b26c01b08).

## Access-event construction

An eight-second Darwin ARM64 `sample` capture of the previous release build
located access-event UUID generation in `getentropy` and timestamp formatting
inside `proxy::handle` with logging disabled. The capture had load-generator
losses and serves as a source-attribution profile. The candidate gates event and
attempt construction on logger configuration while keeping the wire request ID,
trace validation, lease handling and counters available in both modes.
[Stack samples](../bench/program/evidence/telemetry-before-profile-darwin-arm64.txt)
[Profile workload data](../bench/program/evidence/telemetry-before-profile-load.tar.gz).

Build the same release profile for the baseline snapshot and candidate. The
baseline includes compiled routing and YAML. This isolates the access-event
change from the earlier routing and reload work.

```sh
mkdir -p work/telemetry-reference
tar -xzf bench/program/evidence/telemetry-before-source.tar.gz -C work/telemetry-reference
export CARGO_PROFILE_RELEASE_DEBUG=line-tables-only CARGO_PROFILE_RELEASE_STRIP=none
cargo build --manifest-path work/telemetry-reference/Cargo.toml --release --locked --bin cauce
cargo build --release --locked --bin cauce
uv run --no-project --with aiohttp==3.11.18 python bench/program/telemetry.py \
  work/telemetry-reference/target/release/cauce target/release/cauce \
  work/telemetry-campaign --rate 600 --seconds 6 --warmup 2 --repetitions 5
```

The harness randomizes four cells per repetition (two binaries, logging on/off).
It uses one Tokio worker, a one-route cleartext HTTP/1.1 proxy and the same
loopback backend serving 1 KiB responses. On Linux, `--cpus PROXY BACKEND CLIENT`
can pin roles to distinct CPUs. The client checks response size for every offer.
The harness rejects cells with lost arrivals, p99 scheduler lag over 10 ms by
default, discrepant client, proxy or backend counts, excess backend connections,
or unmatched access logs. It reports proxy CPU time per success, scheduled
latency through p99.9 and per-cell resource snapshots. Paired intervals describe
variation across complete pairs. A fixed offered rate measures this workload
only.

Both campaigns ran at 600 scheduled requests/s on an Apple ARM64 Mac running macOS 15.8.1 and Rust 1.88. The first run completed 100,800 requests across 28 cells. Its socket capacity probe used the application port and could add a connection to the lifetime peak. The revised harness uses the dedicated stats listener and resets that peak after each warm-up. The first run and its exact harness are archived for inspection. [First campaign](../bench/program/evidence/telemetry-darwin-arm64-first-pass.tar.gz).

The revised run completed all 72,000 scheduled requests and logged all 48,010 expected access events in the logging-enabled cells, including warm-up and probes. Nineteen of 20 cells exceeded the 10 ms p99 generator lag limit, leaving zero valid pairs. Keep the implementation as a candidate because the observed work is eliminated and functional checks passed. The current campaigns leave end-to-end CPU and latency effects unverified. [Revised campaign and per-offer data](../bench/program/evidence/telemetry-darwin-arm64-retry.tar.gz).

## Reference implementation review

- NGINX constructs static location trees separately from its server-name structures. Its location semantics include modifiers and regex phases. Cauce's first index compiles its existing exact-authority and prefix rules. [NGINX HTTP initialization](https://github.com/nginx/nginx/blob/release-1.30.8/src/http/ngx_http.c).
- Pingora initializes timers on first pending poll and groups deadlines by clock
  tick. Its pool looks up idle connections by group. Measure Cauce allocation
  and wake-up rates, lock waiting and capacity before changing timers or the
  pool. [Pinned Pingora timeout source](https://github.com/cloudflare/pingora/blob/f86ffda5d3ae5c3baf5125682138b33b26c01b08/pingora-timeout/src/lib.rs) [Pinned Pingora pool source](https://github.com/cloudflare/pingora/blob/f86ffda5d3ae5c3baf5125682138b33b26c01b08/pingora-pool/src/lib.rs).
- HAProxy 3.4.8 checks thread-local idle connections before searching other
  threads within the same group. Envoy separates pools by protocol and drains
  HTTP/2 connections on GOAWAY. NGINX uses its upstream keepalive cache to
  retain idle connections and limits active connections separately.
  [HAProxy 3.4.8 source archive](https://www.haproxy.org/download/3.4/src/haproxy-3.4.8.tar.gz) [Envoy pool architecture](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/connection_pooling) [NGINX keepalive source](https://github.com/nginx/nginx/blob/release-1.30.8/src/http/modules/ngx_http_upstream_keepalive_module.c).
- Caddy's reverse proxy and Traefik's service builder are references for configuration lifecycle and backend selection. Varnish's fetch state machine is a reference for future cache and filter stages. [Caddy reverse proxy source](https://github.com/caddyserver/caddy/blob/master/modules/caddyhttp/reverseproxy/reverseproxy.go) [Traefik service source](https://github.com/traefik/traefik/blob/master/pkg/server/service/service.go) [Varnish fetch source](https://github.com/varnishcache/varnish-cache/blob/master/bin/varnishd/cache/cache_fetch.c).

## Milestone 2 replay

The current baseline uses a Rust open-loop client with persistent HTTP/1.1 and
HTTP/2 connections. Direct preflight and postflight controls qualify the
fixture at twice the tested load. The Milestone 2 report records the
three-way replay, telemetry confidence intervals, capacity gates, Linux
software profiling and rejected measurements.
[Results](PERFORMANCE-MILESTONE-2.md) [Reproduction](BENCHMARK-METHODOLOGY.md).


## Milestone 2B

The retained candidate removes two string-key copies from the reusable
HTTP/1.1 lease path, replaces the idle-count scan with constant-time
accounting and reuses the JSONL writer buffers. The associated concurrency
changes synchronize Hyper readiness and return canceled opening reservations
to waiters. Qualified native throughput, CPU and latency comparisons remain
open. The platform-specific diagnostics, statistical gates and reproduced
pool behavior are recorded in the [Milestone 2B report](PERFORMANCE-MILESTONE-2B.md).

## Milestone 2C

The candidate removes allocation from fixed header-name construction and
typed JSONL fields. Endpoint sharding has a startup-only experimental
selector. A Linux ARM64 guest matrix verified lease accounting with both
strategies through real HTTP/1.1 and HTTP/2 sockets. Compact remains the
default while native comparisons are pending.

The diagnostic allocation replay and correlated transport trace are
available in the [Milestone 2C report](PERFORMANCE-MILESTONE-2C.md).
Native capacity and multicore scaling curves, hardware profiles and
competitive claims remain blocked by host authorization.
