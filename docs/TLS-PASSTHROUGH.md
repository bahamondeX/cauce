# TLS passthrough

A `proxy.stream.tls_passthrough` entry uses its listener address to select
an ordered list of rules. Each rule matches a case-insensitive exact SNI
name, an advertised case-sensitive ALPN identifier, or both. The first
matching rule wins. A valid ClientHello without a match uses
`default_upstream` if configured. A missing SNI can match an ALPN-only
rule or the default. If neither matches the connection closes.

rustls's server Acceptor parses the ClientHello before a TLS configuration
is chosen. It supports fragmented records within `preread_bytes` and
`preread_timeout`. The proxy saves and forwards the original wire bytes in
order, including any bytes co-read with the ClientHello. The origin owns
TLS negotiation, certificates, client authentication and session keys.
An unsupported extension is interpreted only if rustls decodes it for SNI
or ALPN. TLS extensions that rustls rejects cause the preread to fail.
Encrypted ClientHello exposes at most the outer public name and outer
extensions to routing. Malformed handshakes and non-TLS traffic close
without reaching a default origin.

Inbound PROXY parsing, if enabled, runs before preread and outbound
PROXY framing precedes the saved TLS bytes. See the
[example](../examples/stream.yaml) and
[NGINX ssl_preread semantics](https://nginx.org/en/docs/stream/ngx_stream_ssl_preread_module.html).
