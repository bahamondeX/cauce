# Milestone 7 · Multiprotocol gateway

Cauce runs independent HTTP, HTTPS, TCP, UDP, TLS passthrough and
HTTP/3 listeners. The Layer 4 runtime is checkpointed at `a681045`.
Downstream HTTP/3 and explicit HTTP TCP listeners are checkpointed at
`22d6ac9`. The baseline for runtime comparison is `1ff7f23`.
[Architecture](ARCHITECTURE.md) [Configuration](CONFIGURATION.md).

## Interoperability and security

The stream suite covers TCP half-close, IPv6, Unix upstreams, weighting,
consistent hash, admission, cancellation, slow consumers, deadlines,
passive failure, TLS fragmentation and PROXY v1/v2 validation. UDP cases
cover echo, packet boundaries, DNS, deliberate loss and reordering,
byte and packet reflection budgets, exhaustion and endpoint recovery.
PostgreSQL 14.17 returned a real query, Redis 7.4.9 returned RESP
commands, and OpenSSH 9.9 exchanged the configured Ed25519 host key.

The HTTP/3 suite uses aioquic 1.3.0 and a Node HTTP/2 origin. It verifies
independent TCP and QUIC listeners, SNI, certificate rollback and rotation,
two-way streaming with trailers, concurrent streams, shared cache hits,
early origin cancellation, encoded and decoded field-section rejection,
SIGTERM response draining, UDP source-port rebinding and simultaneous
HTTP, HTTPS, QUIC, TCP and UDP service on one process. The pinned
NGINX 1.30.8 build checks shared Stream and HTTP/3 behaviors.
[Regression evidence](../bench/program/evidence/milestone-7/).

PROXY codec AddressSanitizer fuzzing used 100,000 executions with seed
71126 on Linux ARM64 in Colima. The campaign completed without a crash.
The wire parser bounds headers before upstream connection, and the
datagram relay requires one forwarded request packet for each response
and limits response payload credit to a configurable ratio no higher
than three.

## Regression gates

| Gate | Result |
| --- | --- |
| Formatting, strict Clippy and Rust tests | 66 passed and one ignored lookup benchmark |
| Legacy HTTP and collector | 24 HTTP cases on each pool strategy, 23 collector cases and 13 storage cases |
| Cache concurrency model | Loom publication fence passed for invalidation and generation retirement |
| HTTP/2, gRPC and lifecycle on Colima ARM64 | Protocol interoperability, HUP and TERM, 20 header deadline cases, cleartext and TLS stress |
| TLS and WebSocket lifecycle on Colima ARM64 | Verified certificates, negative hostname and CA cases, 4 MiB fragmented echo, cancellation, quotas and shutdown |
| Content and cache on macOS and Colima ARM64 | Static, range, gzip, Brotli, memory cache and reload checks |
| Legacy NGINX comparisons on Colima ARM64 | 20 routing, 11 gateway cases on each pool strategy, and 15 content cases |
| New protocols | 11 Layer 4 and 9 HTTP/3 cases on macOS and Colima ARM64, including NGINX differential checks. |
| NGINX migration and configuration | 8 migration tests, 10 YAML schema examples and runtime checks of the new YAML and TOML examples |
| Linux release images | ARM64 exercised in Colima. AMD64 passed configuration smoke under emulation. |
| Synthetic Connectivity contract | Nine collector delivery and recovery cases on Colima ARM64 |

The macOS routing fixture produced the same case-sensitive regex
difference against the rebuilt NGINX reference for both baseline and
candidate. The Linux reference passed the pinned routing suite. A
strict certificate fixture required a compatible Python/OpenSSL
combination on macOS. The same mTLS cases passed independently in
the Linux guest.
[Raw regression evidence](../bench/program/evidence/milestone-7/regression.tar.gz)
[Rejected fixture runs](../bench/program/evidence/milestone-7/rejected-fixture-runs.tar.gz).

## Local diagnostics and native runner

`bench/program/network-benchmark.py` produces fixed-rate raw offer rows
and checks sequence accounting and generator jitter. TCP supports
new connections, concurrent full-duplex transfers and slow consumers.
UDP supports new associations and reused client sockets. HTTP/3
records connection handshake timings and multiplexed requests over
pinned, independent client connections. Paired references are Cauce,
NGINX 1.30.8 and HAProxy for TCP. Cauce and NGINX run the generic UDP
and HTTP/3 fixtures. The direct TCP and UDP controls use the same origin.

```sh
python3 bench/program/network-benchmark.py tcp direct work/m7/tcp-control \
  --rate 100 --seconds 10 --size 1024 --cpus 2 4 6
python3 bench/program/network-benchmark.py tcp cauce work/m7/tcp-cauce \
  --proxy target/release/cauce --rate 100 --seconds 10 --size 1024 --cpus 2 4 6
python3 bench/program/network-benchmark.py udp nginx work/m7/udp-nginx \
  --proxy /path/to/nginx --rate 100 --seconds 10 --size 128 --cpus 2 4 6
uv run --no-project --with aioquic==1.3.0 python bench/program/network-benchmark.py \
  http3 cauce work/m7/h3-cauce --proxy target/release/cauce \
  --cert work/http3/tls.pem --key work/http3/tls.key --connections 8 --cpus 2 4 6
```

The network runner exposes the sampled PID and per-cell CPU, memory,
descriptor and Linux context switch snapshots. Use a qualified generator,
an independent direct control, a bounded test network with recorded
latency and packet loss, and separate `perf stat` or syscall sampling
for competitive claims. Record established connection counts and compare
idle, active and post-expiry memory in separate cells. The Python and
aioquic load generator smoke tests established fixture behavior and
accounting at 25 offers/s. All 550 offers in the archived TCP, UDP and
HTTP/3 cells completed. The TCP smoke includes HAProxy 3.2.27. Two initial
UDP cells and one socket-reuse UDP retry exceeded the 10 ms scheduler
p99 gate. [Raw fixture results](../bench/program/evidence/milestone-7/network-smoke.tar.gz).

TCP starts with Tokio's bounded two-buffer bidirectional copy and
configurable nodelay, socket buffers, keepalive and reuseport. UDP
uses connected socket associations. Linux splice, buffer recycling,
vectored I/O, recvmmsg and sendmmsg remain experiments. Determine the
payoff on throughput, slow consumers, loss and syscall count under
qualified native workloads before changing the default algorithms.

## HTTP baseline comparison

The macOS ARM64 HTTP/1.1 run compared release binaries built from
`1ff7f23` and the candidate with the stream and QUIC listeners absent.
One worker used compact pooling and the same native load generator.
At 200 offers/s each measured cell carried 800 requests. All 22,400
measured offers succeeded. Four cells failed the scheduler or HTTP/1.1
latency gate. The qualified logs-off subset yielded six pairs.

| HTTP/1.1 logs off | Baseline | Candidate |
| --- | ---: | ---: |
| Median CPU time per success | 150 µs | 150 µs |
| Median scheduled p99 | 4.12 ms | 4.81 ms |
| Median ending RSS | 4,688 KiB | 4,840 KiB |

The HTTP/1.1 paired median RSS delta was +272 KiB. Its six pairs fell
short of the seven-pair confidence interval gate.

The matching HTTP/2 run qualified all 28 cells and completed all
16,800 offers at the same 200 offers/s. With logs off, the paired
95% interval for CPU spanned −33 to +33 µs per success and the
scheduled p99 interval spanned −0.53 to +0.06 ms. The median paired
ending RSS rose by 592 KiB, with a 95% interval of +224 to +656 KiB.

The Mach-O text segment grew by 1.56 MiB. One separate HTTP/2 run
sampled both binaries after 600 measured requests using macOS vmmap.
The candidate had 464 KiB more resident text and 112 KiB more
physical footprint. Live allocations in the default malloc zone
were 163 KiB for the baseline and 162 KiB for the candidate. This
snapshot does not establish request-path allocation counts or fully
attribute the paired RSS difference. Native performance and the
allocation gate remain open. Disabled listeners start no protocol
workers and the ordinary Hyper request adapter has no Alt-Svc work.
[Raw measurements and qualification](../bench/program/evidence/milestone-7/http-ab-darwin.tar.gz).

## Scope and deployment status

No AWS resources were provisioned. Milestone 6 verified cleanup of its
temporary resources and did not reconcile campaign expenditure.
The native EC2 lab script retains a separate guest shutdown timer and
host-side cleanup with EC2 termination and volume verification. The
local build artifacts provide Linux ARM64 and AMD64 release images.
Native competitive performance, production promotion and 100% NGINX
compatibility remain open.

Remaining independent extensions include shared TTL-driven DNS with
connection retirement, advanced resilience and retry budgets, background
stale-while-revalidate, disk cache quotas and crash recovery, and
automatic HTTPS with ACME. Stream TLS termination, upstream HTTP/3
and WebTransport also remain open.
