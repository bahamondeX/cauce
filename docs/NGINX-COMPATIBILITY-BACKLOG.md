# NGINX OSS compatibility backlog

This is an adoption-focused implementation order based on migration impact.
The [NGINX OSS 1.30.8 registry](../compatibility/nginx-oss.yaml) remains
the source of directive and behavior coverage. Dataplane performance is
the current milestone gate.

| Priority | Capability | First acceptance slice |
| --- | --- | --- |
| 1 | Listener and virtual host routing | Multiple listeners, exact and wildcard names, default server, standard location precedence, explicit handling of unknown authorities, and atomic reload of listeners and certificates. |
| 2 | Request and response transformations | Common `proxy_set_header`, rewrite and redirect behavior, safe trusted proxy forwarding, response header manipulation and an explicit URI mapping contract. |
| 3 | Proxy buffering and upstream lifecycle | Independent request and response limits, bounded disk spooling, proxy deadlines, opt-in retry safety, and dynamic DNS with TTL and draining. |
| 4 | Static sites | Document root, index, MIME types, ranges, conditional requests, directory listing, and symlink and traversal controls. |
| 5 | TLS deployment | Multiple SNI certificates, optional client certificate verification, certificate renewal lifecycle, and verified upstream identity. |
| 6 | Production traffic policies | Rate and connection limiting, active upstream health checks, logging customization and stable operational counters. |
| 7 | Caching and compression | Cache keys, admission and eviction, invalidation, stale use, configurable gzip, range and conditional response interoperability. |
| 8 | TCP, UDP and application gateways | Stream listeners and proxying, then FastCGI and other application protocols within each protocol's resource and timeout contract. |
| 9 | Extended protocols and modules | HTTP/3, add-on module alternatives and the remaining pinned registry entries, with explicit unsupported cases. |

Each migration slice needs equivalent configuration, a differential workload,
negative cases for the boundary it exposes, and an update to the registry.
