# ADR 0009 · Virtual host and location precedence

**Status** Accepted for the literal routing subset.

Compile virtual hosts when preparing a configuration generation. The existing
index holds exact authorities. Separate optional maps hold complete leading
and trailing DNS wildcard labels and group them by the literal authority port.
Select the exact authority, then the longest leading wildcard, then the
longest trailing wildcard. Each virtual host has an optional literal-path
map and the existing segment-aware prefix index. Exact locations take
priority only for their identical path. Preserve the query for forwarding.

The lookup uses borrowed hostname slices and bounded DNS label trials.
Exact-only configurations allocate no wildcard map. Prefix-only hosts
allocate no literal-path map. A single exact-authority route still bypasses
the compiled maps. Invalid patterns or duplicate path modes reject a reload
before publication.

NGINX server defaults, regex names, location modifiers beyond `=`, and URI
normalization require separate compatibility work. Existing Cauce consumers
keep explicit authority port matching and unconfigured hosts return 404.
[NGINX server-name precedence](https://nginx.org/en/docs/http/server_names.html)
[Configuration and example](../CONFIGURATION.md).
