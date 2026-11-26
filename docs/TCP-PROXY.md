# TCP proxy

Configure `proxy.stream.tcp` in YAML or `[[stream.tcp]]` in TOML.
The listener references a `stream.upstreams` group containing literal IPv4
or IPv6 socket addresses or absolute Unix socket paths. Omit `proxy.listen`
and HTTP routes to run a stream-only gateway. See the paired
[YAML](../examples/stream.yaml) and [TOML](../examples/stream.toml) examples.

Each accepted client gets an independent upstream connection. Tokio
`copy_bidirectional_with_sizes` forwards bytes with two bounded buffers.
An EOF shuts down the opposite write half after buffered bytes have drained.
The other direction remains active until EOF, a relay error or a deadline.
Idle time measures a lack of write progress in both directions. The absolute
session deadline starts at accept and includes PROXY parsing, TLS preread,
upstream connection and relay time.

Weighted round robin uses smooth tickets and consistent hash uses weighted
rendezvous of the canonical client IP. A failed connect or upstream I/O
error suppresses new selections for `failure_cooldown`. After cooldown the
endpoint becomes eligible again. Clients establish a new connection after
failure. Per-listener and optional per-socket-peer caps apply before preread.
The configured send and receive buffer sizes are socket requests and the
kernel may clamp them. Each direction uses 8 KiB by default.

`nodelay`, `keepalive_secs`, `receive_buffer_bytes` and
`send_buffer_bytes` apply to TCP sockets. `reuse_port` permits cooperating
processes to bind the same address and admission limits apply independently
in each process. Listener settings and stream upstreams require restart.
SIGHUP rejects changes to those fields before publishing the HTTP generation.
SIGTERM closes accepting sockets and drains sessions up to `shutdown_ms`.

## PROXY protocol

Enable inbound framing with `options.proxy_in`, a CIDR list
`trusted_peers`, an optional `header_timeout` and `max_header_bytes`.
A listener with inbound PROXY requires a header for every connection and
checks trust against the actual socket peer. V1 TCP4/TCP6 and V2
TCP-over-IPv4/IPv6 preserve forwarded source and destination. V1 UNKNOWN
and V2 LOCAL fall back to socket addresses. The v2 decoder validates TLV
framing, nested SSL TLV framing, unique-ID length and CRC32C where present.
The codec consumes metadata for framing and ignores its application-level
assertions. Malformed, oversized, unsupported address/transport combinations
and truncated headers close the stream.

Set `options.proxy_out: v1` or `v2` only where the origin expects that
format. The outbound header precedes all application bytes. It carries
forwarded addresses from a trusted header or the physical socket addresses
and excludes TLVs. TCP admission uses the physical peer, stream hash
selection uses the resolved source and HTTP identity uses its own policy.
[HAProxy wire specification](https://www.haproxy.org/download/2.9/doc/proxy-protocol.txt).

## Stream TLS

Set `stream.tcp[].tls` to terminate downstream TLS. Its PEM default
certificate, `sni` map, TLS versions, client CA and resumption settings
use the same format as HTTP TLS. Set `stream.upstreams[].tls` to
encrypt the upstream connection with an explicit `server_name` and
`ca_file`. A client certificate is optional. DNS addresses keep the
configured upstream hostname and trust policy.

The per-listener `tls_handshake_timeout` defaults to five seconds.
The connect and absolute session limits additionally bound the upstream
handshake. When configured, trusted inbound PROXY is read before TLS
termination and outbound PROXY is written before connecting with TLS.
TLS passthrough remains a separate listener mode. TCP relay preserves
directional shutdown and sends TLS close notifications when the relayed
peer closes cleanly.

The local NGINX OSS 1.30.8 comparison covers default and named
certificates, downstream certificate requirements, upstream mTLS and
upstream hostname rejection. A separate Cauce fixture covers an idle
TLS handshake deadline.

[Stream TLS fixture](../tests/stream_tls.py)
[Listener lifecycle](LISTENER-LIFECYCLE.md).
