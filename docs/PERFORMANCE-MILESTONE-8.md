# Milestone 8 · Application gateways and mail

The application and mail functional candidate starts at `e612f84`. FastCGI
is checkpointed at `ed8f4b8`, uWSGI and SCGI at `6b4bb31`, and mail at
`710188d`. The tested release runtime uses the parser checkpoint
`d2d2dda`. Compact pooling remains the default. Endpoint sharding
still passes the existing HTTP regression suite.

## Functional evidence

| Suite | Verified scope |
| --- | --- |
| FastCGI | Seven test groups. Seven shared cases through PHP-FPM 8.4.22 and NGINX 1.30.8 cover GET, POST, multipart, large responses, redirects, cookies and errors. Independent Unix framing covers reuse, client cancellation, malformed records, truncation and fixed script paths. |
| uWSGI and SCGI | Four test groups. Each wire mode of uWSGI 2.0.21 served a Python WSGI app and matched NGINX on five cases. Unix fixtures independently decode environment frames and verify known-length and buffered uploads, malformed and truncated heads, and subsequent recovery. |
| Mail | Six interoperability and security groups, plus a socket drain case. Postfix 3.7.11 and Dovecot 2.3.19.1 worked with Python SMTP, IMAP and POP3 clients. STARTTLS and STLS exercised the same backends through NGINX. The candidate also passed implicit TLS, SMTP LOGIN, IMAP PLAIN, verified backend TLS, wrong backend hostname, auth service denial, untrusted backend selection, overlong commands, admission, rollback and authenticated SIGTERM draining. |
| Existing regressions | HTTP integration on compact and sharded pools, HTTP/3 with aioquic 1.3.0 and pinned NGINX, TCP and UDP with the pinned Stream reference, content handlers, memory cache, mTLS, collector and collector storage. |
| Configuration | Draft 2020-12 schema and YAML/TOML samples validated. Migration audit reports application and mail semantics for manual review. |

Mail authorization checks both the HTTP service and the selected backend.
The cleartext upgrade stage exposes no usable authentication. The app
gateway tests cover missing and bad framing, script parameter isolation,
bounded request buffering and socket disposal on cancellation.
The application codec fuzz target completed 100,000 AddressSanitizer
executions (seed 381108, maximum input 4 KiB) in the Linux ARM64 guest.
It exercises the production FastCGI header parser, CGI response header
parser and native uWSGI and SCGI prefix encoders.
[ASan log](../bench/program/evidence/milestone-8/application-wire-asan.log.gz).

An initial concurrent Layer 4 run timed out in the IPv6-to-Unix
half-close case and the full isolated rerun passed. An initial SCGI run failed during fixture startup. The updated
readiness gate verifies an actual native response over the backend wire. Mail fixture startup allowance was increased after delayed
Postfix startup during concurrent compilation. The rejected runs are
included with the raw evidence. [Evidence](../bench/program/evidence/milestone-8/).

## Disabled-protocol diagnostics

The existing paired workload uses one worker, one route and one upstream,
verified 1 KiB bodies, and compact pooling. Configurations exclude mail
and application protocols. The same native load generator sends
200 offers/s for three seconds after one second of warm-up. Direct
400 offers/s controls and full client, proxy and origin accounting are
retained. This is a local macOS ARM64 diagnostic.

| HTTP reference and workload | Measured cells | Rejected cells | Valid pairs with logs off / on | Paired median ending RSS delta |
| --- | ---: | ---: | ---: | ---: |
| `e612f84` HTTP/1.1 | 28 | 10 | 3 / 2 | +144 / +200 KiB |
| `e612f84` HTTP/2 | 28 | 3 | 6 / 6 | +208 / +216 KiB |
| `1ff7f23` HTTP/1.1 | 28 | 7 | 3 / 4 | +672 / +656 KiB |

Every comparison fell short of seven valid pairs per logging condition.
The raw records preserve the specific SLO, scheduler and accounting gates.
Matched CPU and tail-latency release qualification remains open.
[Raw diagnostics](../bench/program/evidence/milestone-8/darwin-diagnostics.tar.gz).

### Binary and resident memory

| Darwin build | Release file bytes | Mach-O `__TEXT` bytes | Ending RSS KiB | Physical footprint KiB | Resident text KiB | Live malloc nodes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `1ff7f23` | 7,403,648 | 6,914,048 | 4,112 | 1,777 | 7,696 | 424 |
| `e612f84` | 9,144,736 | 8,552,448 | 4,576 | 1,905 | 8,096 | 424 |
| `d2d2dda` | 10,669,376 | 9,977,856 | 4,736 | 2,001 | 8,240 | 423 |

A separate one-worker HTTP/1.1 session served 200 verified responses per
build. RSS comes from `ps`, resident text and physical footprint from
`vmmap`, and live node count from `heap`. The candidate grew by 1.45 MiB
on disk compared with `e612f84`. The text segment accounts for 1.36 MiB.
The loaded snapshot shows +160 KiB RSS, +96 KiB physical footprint, and
+144 KiB resident text. Live nodes are a point-in-time measure. Allocation
calls per request require a repeated allocator trace.

The runtime dependency lock is unchanged. The dependency inventory
still shows duplicate Base64, getrandom, hashbrown and syn versions in
different dependency families. Mail now uses the HTTP client in the
gateway binary, where the previous tree used it for the collector only.
This is a candidate source of linked-code growth. Mail client, TLS
acceptor, session task and listener preparation are conditional on mail
configuration. Application buffers
are reserved only when a compiled route enables them. The measurements
have no symbol-level attribution. Code generated for the application
transports and mail TLS/authentication paths remains a target for
monomorphization analysis. [Raw binary segments](../bench/program/evidence/milestone-8/binary-footprint.txt).

Optional build profiles are open. HTTP reverse proxy and HTTP with web
and cache would retain the shared HTTP engine. Layer 4 and HTTP/3 can
gate their separate listeners and heavy transport pieces. An application
profile could include CGI transports without mail. The full distribution
should remain the default, and slim builds need explicit validation errors
for unavailable features. These proposed profiles have no measured build
or runtime figures in this checkpoint.

## Application benchmark runner

The network runner accepts `fastcgi`, `uwsgi` and `scgi` with a staged,
loopback backend and an explicitly recorded backend identity. Use the
same backend image, fixtures, CPU placement and limits for both proxies.
The load client verifies request digests, response status, body bytes and
lengths. Raw offers retain bounded loss, timeout and scheduler outcomes.
The runner can vary upload size, response size, origin delay, slow reads
and FastCGI keepalive. It samples CPU, RSS, Linux PSS and private pages,
and Cauce connection counts. Run allocation tracing and native process
counters as separate matched runs.

```sh
python3 bench/program/network-benchmark.py fastcgi cauce work/m8-fastcgi-cauce \
  --proxy ./cauce --origin-port 9000 --origin-identity '<pinned image and fixture hashes>' \
  --fastcgi-keepalive --size 131072 --request-size 131072 --inflight 32 \
  --rate 200 --seconds 60 --warmup 10 --cpus 1 2 3
```

Start the separately pinned backend on the supplied origin core before
running application cells. Repeat with the pinned NGINX 1.30.8 binary.
The local smoke exercised both proxies on all three protocol modes with
8 measured 128 KiB uploads and 128 KiB responses per mode. Those runs
qualify harness behavior. They give no competitive performance result.
[Runner](../bench/program/network-benchmark.py) [Native methodology](PERFORMANCE-NATIVE.md).

## Release boundary

Complete source-wide parity, native competitive performance, optional
builds and sustained resource qualification remain open. The 1.0 backlog
includes shared DNS and resilience, background revalidation and disk
cache, ACME, Stream TLS, and atomic replacement of non-HTTP listeners.
The mailbox, SMTP delivery policy and data remain the responsibility of
the backend. This work created no AWS resources.
[Roadmap](ROADMAP.md#milestone-8-y-puertas-de-cauce-10).

Build identities, fixture digests and SHA-256 evidence checksums accompany
the report. The source tree behind the recorded Milestone 7 Darwin
binary is identical to the `e612f84` runtime tree. [Build identities](../bench/program/evidence/milestone-8/build-identities.json)
[Checksums](../bench/program/evidence/milestone-8/SHA256SUMS).
