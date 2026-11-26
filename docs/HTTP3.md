# Downstream HTTP/3

[The YAML example](../examples/http3.yaml) configures cleartext HTTP on TCP
8080, HTTPS on TCP 8443 and HTTP/3 on UDP 8443. The TCP and UDP sockets
can use the same number. `proxy.listeners` declares HTTP or HTTPS TCP
listeners independently. Existing `proxy.listen` and `proxy.tls`
configurations continue to work. Use `proxy.http3.tls` for independent
credentials or omit it to inherit `proxy.tls`.

Generate a disposable certificate to run the paired examples locally.

```sh
mkdir -p work/http3
openssl req -x509 -newkey rsa:2048 -nodes -days 3 -subj /CN=localhost \
  -addext subjectAltName=DNS:localhost -keyout work/http3/tls.key -out work/http3/tls.pem
chmod 600 work/http3/tls.key
cargo build --locked
target/debug/cauce check examples/http3.yaml
target/debug/cauce examples/http3.yaml
```

A client with HTTP/3 support can use the local CA file.

```sh
curl --http3-only --cacert work/http3/tls.pem https://localhost:8443/
```

The listener uses TLS 1.3, ALPN `h3`, the shared certificate resolver
and optional client certificate verification. Setting `advertise: true`
adds `Alt-Svc` on TLS TCP responses for the configured UDP port. Address
validation uses QUIC Retry. TLS early data is disabled. `path_migration`
permits Quinn's validated path migration and defaults to false. Requests
retain the peer identity captured at connection admission.

HTTP/3 reuses the HTTP routing, identity, admission, static, cache,
compression and proxy policies. The QUIC adapter streams DATA and trailers
with independent flow control. A client that cancels response reception
unblocks the request task even while upstream headers are pending. Request
and connection deadlines bound both I/O and waiting for delivery.
HTTP/3 WebTransport, upstream H3 and gRPC over H3 remain separate extensions.

Admission uses the shared HTTP socket and request budgets and adds limits
for QUIC connections, concurrent bidirectional streams and lifetime
request count. Field sections have encoded and decoded bounds. Control
streams have bounded input. The h3 decoder uses stateless QPACK.
[Transport decision](adr/0015-quinn-http3-transport.md).

SIGHUP prepares certificates and the HTTP generation before publication.
New handshakes use the new TLS settings. Established QUIC connections keep
their TLS and peer identity. Listener additions, address changes, transport
limits, Alt-Svc settings and migration changes require restart. SIGTERM
stops new handshakes, sends GOAWAY, drains admitted streams and closes
connections at the shutdown deadline.

The updated collector and access-log schema accept `HTTP/3` in version
1.1 events. Roll out the collector update before enabling H3 where a
receiver validates the existing protocol enum.
