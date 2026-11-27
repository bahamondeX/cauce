![Cauce: a self-hosted Rust reverse proxy](docs/assets/cauce-cover.svg)

**English** · [Español](README.es.md) · [Product brief (PDF)](docs/CAUCE-brief.pdf)

Cauce is an Apache-2.0 reverse proxy for teams that want one front door for HTTP APIs and long-lived streams. Run it on your infrastructure, describe routes in versioned YAML, and see how traffic moves through your services.

## What you can do

| Need | What the public build provides |
| --- | --- |
| Put one entry point in front of several services | Exact host and longest path-prefix routing, round-robin or least-inflight balancing, and pooled upstream connections over TCP or Unix sockets. |
| Serve more than short HTTP requests | HTTP/1.1, HTTP/2, gRPC, WebSocket upgrades, and Server-Sent Events. Bodies can stream without buffering the whole response. |
| Change routes without an avoidable restart | Check YAML or legacy TOML offline. Reload compatible configuration and TLS material atomically; an invalid candidate leaves the current configuration running. |
| Operate it yourself | Inbound HTTPS, configurable upstream TLS with certificate and hostname verification, graceful draining, Prometheus metrics, and optional structured JSONL access logs and collector. |

## Try it locally

With Docker and Docker Compose:

```sh
git clone https://github.com/bahamondeX/cauce.git
cd cauce
docker compose up --build -d
curl -fsS http://127.0.0.1:8080/
curl -NsS --max-time 5 http://127.0.0.1:8080/sse
docker compose down
```

> **Collector safety:** Use the default Compose profile for this quickstart. Keep the optional SQLite log collector disabled until a patched public build is available. See [RUSTSEC-2026-0459](https://rustsec.org/advisories/RUSTSEC-2026-0459.html).

The example exposes the proxy on loopback and includes sample backends. The SSE example uses a five-second limit so it does not hold the terminal open. For a native build with Rust 1.88 or newer:

```sh
cargo build --release --locked
cargo run --locked -- check examples/cauce.yaml
cargo test --locked
```

## Evidence and boundaries

The [public engineering record](STATUS.md) includes 44 Rust unit tests and 22 real-socket integration checks on macOS ARM64 for the documented candidate, plus scoped Linux ARM64 container checks. Those results are not a production throughput guarantee.

The public build is an **HTTP reverse proxy**. It does not ship downstream HTTP/3, mutual TLS, response caching, static file serving, generic TCP/UDP proxying, or FastCGI/uWSGI in this version. No blanket configuration parity, performance win, or 1.0 release is claimed.

[Configuration](docs/CONFIGURATION.md) · [Architecture](docs/ARCHITECTURE.md) · [HTTP/2 and gRPC](docs/HTTP2.md) · [Observability](docs/LOGGING.md) · [License](LICENSE)

A separate [historical AMD64 benchmark](docs/CAUCE-benchmark-note.md) documents a CPU/latency tradeoff for an unreleased build, not the public release.
