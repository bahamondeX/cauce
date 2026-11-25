# Active HTTP health checks (opt-in)

This branch adds active readiness checks for **HTTP/1 upstreams**, including Unix
sockets and TLS using the upstream's configured CA and server name. It does not
implement gRPC Health Checking or HTTP/2 probes; enabling a check for an HTTP/2
upstream is rejected rather than silently probing a different protocol.

Add under an existing `[[upstreams]]` entry:

```toml
[upstreams.health_check]
path = "/ready"
interval_ms = 1000
timeout_ms = 500
rise = 2
fall = 3
```

Omit the table to preserve existing behavior. Each enabled upstream starts
unavailable until `rise` consecutive successful probes. Only 2xx headers count
as success; 3xx is not followed, and errors/timeouts/non-2xx count as failures.
After `fall` consecutive failures it is excluded from new routing selections.
Success and failure streaks reset each other. Passive failure backoff remains
independent and must also have elapsed before selection. In-flight requests are
not terminated by health transitions. If every candidate is unavailable, the
existing proxy behavior returns 503. Admin `/ready` still reports process
readiness, not application health of every upstream.

Probes use GET, configured Host, and `Connection: close`. No client credentials,
forwarded headers, request bodies or response bodies are copied. Use a dedicated
safe, cheap readiness endpoint. A response body is not consumed: success means
that successful response headers arrived, not that a full response was received.

## Resource and lifecycle limits

- At most 64 configured checked upstreams and four concurrent probes per current
  generation, using a separate semaphore, never the request connection pool.
  These may add four sockets beyond the configured proxy pool limit.
- Total probe timeout 10–5000 ms, covering DNS/connect/TLS/HTTP headers.
- Interval 100–60000 ms, at least the timeout; rise/fall each 1–10.
- At most 8192 bytes of HTTP parser buffer and 32 response headers.
- Interval is a delay **after** each completed probe. Queueing behind the semaphore
  is not counted as failure, and can increase detection latency. No catch-up bursts.
- Reload cancels old probe tasks and starts fresh fail-closed state; existing
  request draining behavior is unchanged. Shutdown cancels and joins the manager.
  No detached HTTP driver task is created.
- Paths must be bounded origin-form paths, not URLs. Configuration errors reject
  startup/reload. No network probe is made by `--check`.

## Verification

The isolated Docker check target runs formatting, Clippy with warnings denied,
and all Rust tests. Added tests exercise streaks/default compatibility, invalid
config, actual 200/204/302/503 responses, header-only completion, stalled response
timeout, routing ejection/recovery, and scheduler cancellation. Existing proxy,
HTTP/2 pool and collector tests remain part of that suite. See
`active-health-checks.log` for the final run.

TLS and Unix use existing transport primitives; this change does not yet have a
new dedicated certificate-rotation, DNS-exhaustion or Unix-socket health-probe
stress suite. No production performance, deployment, or full-application
readiness guarantee is claimed. This change has now been promoted locally into the original integration tree;
no service was redeployed.

### Initial branch result and merge gate (historical)

Formatting and Clippy passed. All 26 proxy tests passed, including the four new
health tests, twice. The full build is **not green**: the unchanged collector test
`lost_unread_log_fails_even_when_spool_is_full` failed in both final attempts
(10 collector tests passed, one failed). The same baseline is being tested
separately to establish whether this predates the feature. Do not describe the
full regression suite as passed or merge automatically until this is resolved.

## Combined branch validation

The collector gate described above is resolved in the isolated combined branch.
All 39 Rust tests and all eight real-adapter integration scenarios pass. See
[combined validation](COMBINED-VALIDATION.md). The original tree has now received the validated changes with reversible backups.
