# Milestone 3 native Linux performance

The native campaign uses `47da8fe` as the Cauce performance reference.
Wildcard virtual hosts, exact locations and byte units are recorded separately
in the functional checkpoint. Compact pooling remains the production
default. [Functional parity](FUNCTIONAL-PARITY.md).

## Method and scope

The controller built or extracted architecture-matched artifacts before
launch. Both hosts use the staged Rust open-loop generator with pooled
retries disabled, Node 24.11.1, and checksummed proxy binaries. The first
pass prioritizes one-worker HTTP/1.1 cleartext without access logs. It
runs adaptive ramps, direct backend controls at twice each new offered
rate, at least 60-second confirmations and randomized
matched-rate pairs. Proxy, generator, backend and sampler use disjoint
Linux CPU sets on the same virtualized EC2 guest.

The common workload is a bodyless GET with a verified 1 KiB response. It
uses HTTP/1.1 upstream reuse, buffering disabled, no configured request
retries, and a 32-request admission envelope. Cauce and NGINX differ in
their implementation of connection lifetime, absolute deadlines and
request identity. HAProxy queues excess requests while Pingora rejects
them before upstream work. Capacity regions where those policies take
effect have different workload contracts. The Cauce JSONL format has
additional fields and different buffering semantics, so the disabled-log
results have their own scope.
[Benchmark gates](BENCHMARK-METHODOLOGY.md)
[Staged runner](../bench/program/native-run-staged.sh)
[Raw offer auditor](../bench/program/audit-native-first-pass.py).

All accepted HTTP/1.1 cells require p99 ≤ 20 ms, p99.9 ≤ 100 ms,
at least 99% offer-window goodput, zero unexpected errors and complete
accounting. A failed direct control withholds the associated capacity
claim. A confirmed rate without a tested failing bound is a lower bound.
Matched CPU, memory and latency comparisons use at least seven valid
pairs and paired bootstrap intervals. The practical CPU threshold is 5%.
Software and allocation profiling run after the comparative cells.

## AMD64 results

The measured host was a virtualized EC2 `c7a.4xlarge` with 16
single-threaded vCPUs, Amazon Linux 2023, and Linux 6.18. The host passed
the CPU isolation check. The kernel exposed cycle, instruction and
branch counters. The generic L1 miss event counted zero in the probe.
The LLC miss event returned `ENOENT`. The guest did not expose per-core
frequency or governor files. Host snapshots retain CPU steal counters
and kernel configuration. The runner did not resolve the proxy's cgroup
CPU limit from `cpu.max`.
[Host facts and counter probe](../bench/program/evidence/milestone-3/amd64-primary-metadata.tar.gz).

### Matched HTTP/1.1 cleartext, one worker, logs disabled

All seven pairs at 1,000 offers/s qualified. The independent raw audit
rechecked 350,000 measured offers and 140,000 warm-up offers across the
14 target cells and their 28 direct backend controls.

| Metric | Cauce | NGINX OSS 1.30.8 | Paired difference and 95% interval |
| --- | ---: | ---: | --- |
| CPU µs per success | 40.0 | 46.0 | −6.0 µs [−6.0, −6.0] |
| Sampled peak RSS | 5.73 MiB | 6.04 MiB | −0.28 MiB [−0.36, −0.25] |
| End PSS | 3.87 MiB | 6.07 MiB | −2.19 MiB [−2.27, −2.16] |
| p50 | 117.48 µs | 115.70 µs | +1.85 µs [+0.05, +2.60] |
| p95 | 136.11 µs | 131.58 µs | +3.62 µs [+2.05, +5.25] |
| p99 | 152.90 µs | 147.03 µs | +5.52 µs [+3.31, +10.26] |
| p99.9 | 968.85 µs | 980.23 µs | +239.14 µs [−194.92, +575.29] |

Differences are medians of within-pair differences. The CPU interval reflects
the host's 100 Hz process accounting and five-second measurement windows.
Cauce used 13.0% less CPU per success and had a higher p99. The p99.9
difference was inconclusive. Process PSS and RSS cover the measured proxy
process. This campaign leaves isolated proxy cgroup memory open.
The workload did not clear all competitive parity targets.
[Paired statistics](../bench/program/evidence/milestone-3/amd64-paired.json)
[Independent offer counts](../bench/program/evidence/milestone-3/amd64-matched-audit.json)
[Matched raw offers](../bench/program/evidence/milestone-3/amd64-matched-raw.tar.gz).

### Capacity, scaling and transport

The independent auditor rechecked 7,572,500 measured offers in the primary
archive and 915,000 in the supplement. The table includes only confirmed
goodput. Each confirmation lasted 60 seconds.

| HTTP workload | Workers | Confirmed successes/s | Gate or observed limit |
| --- | ---: | ---: | --- |
| Cauce HTTP/1.1 | 1 | 7,500 | The 8,000/s ramp failed its warm-up. |
| Cauce HTTP/1.1 | 2 | — | All 900,000 target offers completed at 15,000/s. The postflight failed. |
| Cauce HTTP/1.1 | 4 | — | 42 generator queue rejections at 16,000/s. |
| Cauce HTTP/1.1 | 8 | ≥8,000 | Direct control failed at 16,000/s. |
| NGINX HTTP/1.1 | 1 | ≥8,000 | Conservative run passed both controls. Earlier 16,000/s confirmation and postflight failed. |
| Cauce HTTP/2 cleartext | 1 | ≥2,000 | Direct control failed at 4,000/s. |

Lower-rate Cauce runs with two and four workers failed direct controls
before confirmation. The four-worker stability hold never qualified.
The available one- and eight-worker points do not establish multicore
efficiency or a comparable saturation boundary for Cauce and NGINX.
[Capacity and scaling figure](../bench/program/evidence/milestone-3/amd64-capacity-and-scaling.svg)
[Capacity points](../bench/program/evidence/milestone-3/amd64-capacity-curve.csv)
[Scaling data](../bench/program/evidence/milestone-3/amd64-primary-scaling.csv)
[Primary raw audit](../bench/program/evidence/milestone-3/amd64-primary-audit.json)
[Supplemental raw audit](../bench/program/evidence/milestone-3/amd64-supplement-audit.json).

All ten primary pool cells completed under their matched-rate gates
for both strategies at 1,000/s with 1, 4, 16, 64 and 256 logical endpoints.
Those cells used the `47da8fe` runtime. They do not provide repeated
pool comparisons or a threshold for adaptive selection. The retained
sharded pressure-scan correction has separate real-socket regression
evidence. [Pool strategy and correction](adr/0008-endpoint-sharded-pool.md).

The AMD64 two-worker replay accounted for 8,000 measured and 1,200 warm-up
requests at 400/s with zero transport errors. Both twice-rate controls passed.
The p99.9 was 1.93 ms. The replay retained 165,622 diagnostic events
without dropping any, including correlated sender readiness and response
stages. The guest scheduler sampler ran, but the socket filter was rejected
by iproute2. As a result this replay provides no socket queue or TCP
retransmission evidence. The filter now passes a Linux regression on
established loopback sockets. Root cause attribution for the historical
five failures, the Heaptrack EOF and the earlier tail breach remains open.
[Capacity and replay metadata](../bench/program/evidence/milestone-3/amd64-primary-metadata.tar.gz)
[Kernel sampler regression](../bench/program/evidence/milestone-3/kernel-sampler-regression.tar.gz).

### Separate profiler runs

At 2,500 offers/s, the PMU diagnostic recorded 12.83 billion scaled
cycles, 8.74 billion scaled instructions and IPC of 0.68. Branch misses
were 7.77% of branches. The generic cache-miss event ran for only 21%
of the counter window. CPU migrations counted zero. The diagnostic
load recorded 151 generator queue rejections and the perf window was
shorter than the load window, so these counters cannot establish
resource cost per completed request.

The separate tracepoint pass counted 354,015 syscall entries, 339
futex entries and zero `epoll_pwait` entries for that event. Other
epoll variants were outside its event set. Software sampling captured
1,028 stacks during a successful diagnostic load. The AMD64 executable
was stripped, which prevented a defensible function ranking. The
profile archive retains the raw perf data, stacks and counter windows.
Native allocation profiling remains open.
[Profile archive](../bench/program/evidence/milestone-3/amd64-profile.tar.gz).

### Competitive coverage

| Workload | Cauce | NGINX OSS 1.30.8 | HAProxy 3.4.8 and Pingora 0.9.1 |
| --- | --- | --- | --- |
| HTTP/1.1 cleartext, one worker, logging disabled | Qualified at matched rate. Capacity confirmation at 7,500/s. | Qualified at matched rate. Capacity lower bound at 8,000/s. | Staged fixtures await native qualification. |
| HTTP/1.1 multiple upstreams | Ten compact/sharded observations, up to 256 endpoints. | Open. | Open. |
| HTTP/2 cleartext | Capacity lower bound at 2,000/s. | Cauce h2c workload requires separate interoperability qualification. | Open. |
| Equivalent logging, HTTP/2 TLS, gRPC, TLS reuse, large bodies and overload | Open for native performance. Local TLS, HTTP/2 and gRPC regression gates passed. | Open. | Open. |
| Published Cauce `ba6f952` | Staged on ARM64. The AMD64 results use `47da8fe`. | — | — |

## ARM64 status

Two virtualized Graviton `c7g.4xlarge` launches reached EC2 but never
accepted SSH. The second also failed to register in Systems Manager.
The available boot logs did not identify the cause. Both attempts were
terminated and their tagged cleanup was verified through EC2. The next
staged ARM64 attempt uses the Amazon Linux 2023 image that matches the
working AMD64 setup. Its input bundle is built and checksummed.
Restoring access to the original AWS account and verifying AMD64 cleanup
are prerequisites for launch. There are no qualified native ARM64
measurements for this checkpoint.
[Environment manifests](../bench/program/evidence/milestone-3/native-environments.json).

## Local regression scope

The routing lookup probe compiles the old and new lookup source with
the same optimized compiler and runs seven randomized pairs per
disabled configuration. It measures isolated lookup cost. The first
single-route implementation added a small positive cost. Restoring the
single exact-authority prefix fast path removed a conditional from that
path. The final paired intervals span zero in both tested disabled
configurations. Enabled wildcard and exact-path costs are available as
standalone samples. The measurements leave native request-level overhead
open.
[Raw lookup samples](../bench/program/evidence/milestone-3/routing-lookup.json)
[Probe sources and compiler](../bench/program/evidence/milestone-3/routing-lookup-source.tar.gz).

The checkpoint passed formatting, strict Clippy, serial all-target
Rust tests, real-socket integration on both pool strategies, collector
recovery and storage, Linux resilience, HTTP/2 and gRPC lifecycle,
TLS and WebSocket lifecycle, migration and schema validation. The HTTP/2 header observer
completed 100,000 AddressSanitizer fuzz executions without a crash.
[Regression evidence](../bench/program/evidence/milestone-3/local-regression.tar.gz)
[TLS and WebSocket evidence](../bench/program/evidence/milestone-3/tls-websocket-regression.tar.gz)
[Sanitized fuzz log](../bench/program/evidence/milestone-3/asan-h2-headers.log.gz).

## AWS controls

The lab used tagged one-shot EC2 instances, 40 GiB encrypted gp3 roots
with deletion on termination, and scoped temporary SSH ingress. The
controller staged the data and verified the AMD64 guest poweroff timer
before starting its campaign. Each launch followed termination of the
previous host. The authenticated controller observed both ARM64
instances terminated with zero lab-tagged volumes, interfaces and
security groups.

The AMD64 guest accepted poweroff at **06:24:04 UTC on November 26,
2026**, after both guest archive checksums were verified. SSH timed
out on the independent follow-up probe. The guest was configured for
instance-initiated shutdown to terminate and had a separate verified
06:31:30 UTC poweroff timer. The AWS session expired and the available
browser session selected a different account. EC2 confirmation of
AMD64 termination and root/ENI deletion, deletion of the temporary
security group, and removal of the temporary Systems Manager role and
instance profile remain pending in the authorized account.

The refreshed list-rate projection was **$3.78** for two hours on each
host, with gp3, IPv4, a 2 GB egress allowance and a 25% tax reserve.
A further $0.25 was reserved for failed startup attempts. This remained
below the $10 campaign authorization. The durations through the two
ARM64 termination requests and the AMD64 guest poweroff yield a
list-rate model of **$2.14**, including the same egress and tax
reserves. Using the later AMD64 tag expiration yields **$2.47** under
the same assumptions. Invoice charges and account-specific tax
treatment are unverified.
[Resource lifecycle and model](../bench/program/evidence/milestone-3/aws-lifecycle.json)
[AWS On-Demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/)
[AWS IPv4 pricing](https://aws.amazon.com/vpc/pricing/)
[AWS tax treatment for Peru](https://aws.amazon.com/tax-help/peru/).

The complete AMD64 primary and supplemental offer archives are stored
locally in `work/milestone-3`. Their guest-verified digests are in the
archive index. The paired comparisons, host facts, profiles and
independent audit summaries are checked in.
[Archive index](../bench/program/evidence/milestone-3/raw-archives.json)
[Repository evidence checksums](../bench/program/evidence/milestone-3/SHA256SUMS).
