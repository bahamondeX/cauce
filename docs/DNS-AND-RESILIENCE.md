# DNS and upstream resilience

Set `proxy.dns.servers` to enable shared asynchronous recursive resolution.
Each server is a literal IP and port. Configure a trusted recursive DNS service
inside the deployment boundary. Literal IP and Unix socket upstreams bypass DNS.

One process-wide resolver is shared by HTTP and application upstream sockets,
HTTP active probes, new Stream TCP and UDP sessions, and the HTTPS mail
authentication service. Configure Stream destinations with `dns_address`.
Mail backend selection still uses literal Auth-Server and Auth-Port allowlist
entries. This preserves the authentication service's backend authorization
boundary. Resolver setting changes require restart.

Hickory 0.26.4 supplies parallel A and AAAA lookups, CNAME traversal,
UDP/TCP DNS transport and SOA-based negative caching. Cauce uses the returned
address TTL, limits a result to 64 IPs, and shares one caller-driven refresh
per hostname. The timeout includes waiting for that refresh and for the
global query gate. A cancelled owner releases both locks and permits.
Transient failures apply a short per-host cooldown. Expired addresses fail
closed on resolution failure. A cached hostname may be evicted to admit a
new hostname, which also retires its published address tokens. Configure
`max_hosts` for the number of active names. The default is 1,024.

Refresh is triggered by traffic and configured HTTP active probes. An address
removed from the answer set becomes ineligible for new pooled leases and
HTTP/2 streams. Idle sockets are discarded before reuse or by periodic pool
maintenance. Active HTTP requests and Stream sessions retain their connections.
Upstream TLS uses the configured server identity and trust roots. If
`proxy.dns` is absent, HTTP retains its OS resolver behavior and administrative
`dns_cache_ms` setting. A lookup deadline applies independently of the
upstream TCP and TLS deadlines and is subject to the request deadline.

Existing per-endpoint passive backoff and opt-in HTTP, HTTP/2 and gRPC
health probes continue to control selection. DNS-based updates do not replay
HTTP, application or opaque Stream or mail traffic.

[Hickory resolver documentation](https://docs.rs/hickory-resolver/0.26.4/hickory_resolver/)
[Resolver fixture](../src/dns.rs)
[Socket tests](../tests/dns_upstream.py).
