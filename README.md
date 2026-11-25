# Cauce

Cauce is a self-contained Rust reverse proxy for HTTP/1.1, HTTP/2, gRPC, WebSockets and Server-Sent Events. It runs as a single Tokio process and is licensed under Apache-2.0.

- **Routing and load balancing:** exact host and longest path-prefix matching, round-robin or least-inflight balancing, and pooled connections to HTTP backends over TCP or Unix sockets.
- **Streaming:** bidirectional gRPC, WebSocket upgrades, SSE and streamed bodies without buffering the entire response.
- **TLS:** HTTPS on the inbound listener and optional TLS to upstreams with certificate and hostname verification.
- **Operations:** validated configuration, atomic reload of routes and TLS material, graceful draining, opt-in active HTTP/1 health checks, and passive failure backoff.
- **Observability:** local health endpoints, Prometheus metrics, structured JSONL access logs with rotation, and a separate optional log collector.

## Quick start

With Docker and Compose:

```sh
docker compose up --build -d
curl http://localhost:8080/
curl -N http://localhost:8080/sse
docker compose down
```

The example binds to loopback and includes sample backends. For native builds use Rust 1.88 or newer:

```sh
cargo test --locked
cargo build --release --locked
cargo run --locked -- check examples/cauce.toml
```

See [configuration examples](examples/), [architecture](docs/ARCHITECTURE.md), [HTTP/2 and gRPC](docs/HTTP2.md), [active health checks](docs/ACTIVE-HEALTH.md), and [logging](docs/LOGGING.md). This source release includes test and benchmark runners; generated run artifacts are not bundled.

## Scope

Cauce currently focuses on HTTP reverse proxying. HTTP/3, mTLS, response caching, static file serving, generic TCP/UDP proxying, and FastCGI/uwsgi are not implemented in this release. See the [roadmap](docs/ROADMAP.md).
