# ADR 0014 · Independent TCP and UDP listeners

Bind all configured sockets before the gateway enters its accept loop.
Stream TCP and UDP listeners own their task sets and admission budgets.
They share configuration, signal handling, the peer gate, weighting
primitives and the authenticated metrics endpoint with the HTTP runtime.
Health and selection state is separate by upstream group and transport.
The original HTTP route pipeline has no stream transport calls.

NGINX Stream's full-duplex relay, configurable preread and independent
upstream modules informed the boundary. HAProxy's TCP mode and PROXY
framing distinguish peer metadata from forwarded application data.
Cauce uses bounded Tokio copying initially. Benchmark buffer sizing and
socket options before considering reusable allocation, vectored I/O or
Linux splice. A correct splice implementation would need an independent
half-close, cancellation, nonblocking fallback and pipe resource budget.

UDP gives each listener a socket and each admitted tuple a connected
upstream socket. The design isolates reply associations and can recover
each session independently. It spends a descriptor and one task per
association. Benchmark that tradeoff and queue loss before introducing
a shared-socket demultiplexer or Linux recvmmsg/sendmmsg batching.
Reuseport is an opt-in multiprocess facility with independent budgets.

Stream bindings, routing and options require restart in this increment.
SIGHUP checks these fields for equality before preparing HTTP updates.
This provides whole-generation rollback without replacing live UDP
associations or applying new routing to established streams.

[NGINX Stream proxy](https://nginx.org/en/docs/stream/ngx_stream_proxy_module.html)
[HAProxy configuration reference](https://docs.haproxy.org/3.2/configuration.html).
