# Proxy security contract

Cauce accepts origin-form HTTP/1.1 and configured HTTP/2 traffic for explicit authorities. Configured upstreams determine destinations. The data plane rejects CONNECT and ambiguous HTTP framing/path inputs, strips inbound forwarding identity, and writes its own forwarding headers. The compiled route index runs after the existing authority and path checks.

Inbound and upstream TLS use rustls. Upstream TLS verifies the configured DNS identity and certificate chain against the configured CA. Protect the PEM files, the configuration directory and any environment that supplies administrative credentials. The admin listener binds loopback and exposes health and metrics for local operation. See the [threat model](THREAT-MODEL.md).

Global, per-endpoint and per-connection quotas bound accepted clients, requests, opens, transports, idle sockets and HTTP/2 streams. Request bounds and stream backpressure apply during transfer. A completed HTTP/1 upload and complete response are required before reuse. HTTP/2 connection reuse stays inside its configured endpoint and generation. Retired generations drain existing work and do not share pooled transports with new generations.

Reload candidates must pass parse and transport configuration validation before activation. Existing workers continue using the last valid generation after a rejected reload. Configuration changes that require restart are checked by `Config::reload_compatible`. The collector has separate local storage, authentication, and egress contracts. [Collector operations](COLLECTOR.md).

Review proxy framing, path and header policy, TLS lifecycle, cross-generation pool isolation, and resource limits when changing the request path. Baseline and regression outcomes are tracked in [program status](../STATUS.md).
