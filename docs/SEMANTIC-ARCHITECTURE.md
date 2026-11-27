# Semantic architecture

## Configuration and execution

`Config::load` reads YAML or TOML and compiles a candidate away from
request workers. `location_parents` validates the host, bounded depth
and ancestry. `merge_location` now visits each location once in
parent-first order, including when declarations are out of order.
The merge distinguishes absence from explicit values. An empty
response-addition list replaces inherited additions. An explicit
`internal: false` disables a native inherited restriction. Header
edits, additions, CIDRs, rate zones and route entry each retain their
own merge behavior. Compilation validates the effective values before
publishing the immutable generation.

An external request selects a route and passes its entry guard before
location actions run. The guard returns 404 for `internal: true`.
Only the server's existing typed rewrite and named-routing actions can
enter a guarded location after this step. Client-provided forwarding or
identity headers never grant internal entry. The same finite redirect
budget applies. The final route continues to apply access, admission,
content, filtering and telemetry through the existing shared pipeline.

HTTP, server-wide and protocol scope rules still need separate
implementations. Inheritance of proxy, TLS, logging and Stream/mail
configuration is outside this location compiler.

## Compiled expressions

Direct content accepts `body_expression` independently of literal
`body`. The program supports `$host`, `$uri`, `$request_method`,
`$args`, `$scheme`, `$remote_addr`, `$request_id` and `$$`.
The compiler uses typed identifiers, up to 64 operations, a 4 KiB
source limit and a 16 KiB output limit. Undefined and out-of-phase
variables reject the candidate. The literal path retains its existing
prebuilt bytes. The evaluator reads the request values on demand and
observes the current URI and arguments after a rewrite.

The first consumer is direct response bodies. User-defined variables,
maps, named captures, upstream and response variables, and
header interpolation still require their own lifetime and publication
rules. The expression compiler accepts bare variable names. YAML's
existing whole-value environment references remain separate.

## Proof and dependency graph

`scripts/nginx-semantic-graph.py` derives a dependency graph from
the fixed registry and `compatibility/semantic-foundations.json`.
Group impact is an estimate of registrations that depend on a shared
component. Multiple groups can own the same dependency. Counts are
neither additive nor evidence of implementation.
The graph puts configuration ahead of other groups with 717 OSS
registrations assigned through direct and transitive dependencies.
Variables, regex and execution phases have 343, 325 and 322 potential
dependents respectively.

`scripts/nginx-compatibility.py manifest REGISTRATION` returns
source context, current assessment and evidence. Unreviewed entries
return explicit pending obligations. Reviewed contracts in
`compatibility/verification-manifests.json` specify concrete cases.
The `test --registration REGISTRATION --source COMMIT` mode captures
raw and normalized outcomes from both engines, generated configs,
binary digests and the committed fixture and harness identities.
`scripts/nginx-gap-audit.py` checks these identities and the named
positive, negative and lifecycle obligations before accepting a
promotion. A whole-module promotion requires separate proof.

## PCRE2 evaluation

The current paths use bounded byte-oriented Rust regex and leave
literal and prefix lookups unchanged. The PCRE2 Rust wrapper offers
compiled byte patterns and JIT control. PCRE2 supports interpreter
match, depth and heap limits. A compatibility implementation still
needs per-request work across candidates, explicit rejection on engine
errors, capture provenance through rewrites, and cross-target
packaging. PCRE2 has no active dataplane path in this checkpoint.
[Rust wrapper](https://docs.rs/pcre2/0.2.11/pcre2/bytes/struct.RegexBuilder.html)
[PCRE2 resource controls](https://www.pcre.org/current/doc/html/pcre2pattern.html).

[Source audit](MILESTONE-10-SOURCE-AUDIT.md)
[Machine-readable graph](../compatibility/semantic-dependency-graph.json).
