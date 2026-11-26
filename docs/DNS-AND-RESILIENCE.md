# DNS and upstream resilience

Set `proxy.dns.servers` to enable shared asynchronous recursive resolution.
Each server is a literal IP and port. Configure a trusted recursive DNS service
inside the deployment boundary. Literal IP and Unix socket upstreams bypass DNS.

One process-wide resolver is shared by HTTP and application upstream sockets,
HTTP active probes, new Stream TCP and UDP sessions, and the HTTPS mail
authentication service. Configure Stream destinations with `dns_address`.
Mail authorizes the literal Auth-Server and Auth-Port allowlist entry first.
An operator-defined `dns_address` can supply the dial destination for that
entry if verified backend TLS is configured. Resolver setting changes require restart.

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

## Passive ejection and connection retries

HTTP and application endpoints can set `resilience` with a consecutive
failure threshold, maximum backoff and optional ejection for HTTP 502–504.
The existing `failure_backoff_ms` supplies the base. Backoff increases
after failed recovery and uses per-endpoint jitter. One request at a time
can probe an ejected endpoint. A valid upstream response resets the
counter, and cancellation releases the recovery permit after a cooldown.
Configured active health probes remain a separate selection requirement.
TCP, UDP and mail retain their existing transport-specific failure behavior.

Routes with two or more upstreams of the same protocol can opt into
`connect_retry` with `rate` attempts per second and a `concurrent` cap.
Only empty GET, HEAD and OPTIONS requests can make one alternate acquisition,
after a confirmed transport establishment failure and within the original
request deadline. Each retry consumes a rate token and an acquisition slot.
No stage retries a dispatched request or copies a streaming request body.

[Real-socket tests](../tests/upstream_resilience.py)
[Envoy circuit breaking](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/upstream/circuit_breaking.html)
[HAProxy retry policies](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/retries/)
[NGINX proxy_next_upstream](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream).

Pingora separates connect failure from errors after dispatch. HAProxy
offers additional redispatch and response-based triggers. Envoy accounts
for retry concurrency and outliers by cluster. Cauce's retry hook is
restricted to one pre-transmission alternate acquisition. Its ejection
state tracks consecutive failures and one half-open probe independently
from the retry budget.
[Pingora failover](https://github.com/cloudflare/pingora/blob/main/docs/user_guide/failover.md).
