# ADR 0015 · Quinn and h3 for downstream HTTP/3

Use Quinn 0.11.12 with rustls, h3 0.0.8 and the h3-quinn 0.0.10 control
stream adapter. Quinn supplies QUIC, congestion control, Retry, transport
flow control and path validation through its Tokio runtime. h3 supplies
HTTP/3 framing and QPACK. Cauce reuses the existing rustls certificate and
client-auth policy. Hyper and the H3 adapter instantiate the same request
engine with their native body types.

Cloudflare quiche also supplies a maintained QUIC and HTTP/3 implementation.
The quiche integration under consideration uses BoringSSL and expects
the application to drive packet I/O and deadlines. Quinn fits Cauce's
Tokio and rustls lifecycle and MSRV with a smaller integration surface.
h3 is MIT licensed, Quinn is MIT or Apache-2.0, and quiche uses BSD-2-Clause.
Competitive CPU, memory and packet processing comparisons require native
Linux measurements.

The transport places hard limits on QUIC flow windows, decoded fields,
encoded non-DATA frame size, control input and tasks. The request stream
adapter observes Quinn STOP_SENDING independently from the response
writer. This cancels the shared HTTP future while waiting on upstream
headers and preserves bounded memory under stalled clients. h3-quinn
drives unidirectional streams. Cauce binds endpoints at startup and
publishes new QUIC server configuration after all certificate checks pass.

[Quinn](https://github.com/quinn-rs/quinn)
[h3](https://github.com/hyperium/h3)
[quiche](https://github.com/cloudflare/quiche).
