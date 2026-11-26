# Programmable extension evaluation

njs remains a separate compatibility track. Its JavaScript hooks,
request objects and module semantics require differential tests against
the pinned njs source. The OSS coverage denominator excludes these rows.

| Option | Fit and required verification |
| --- | --- |
| QuickJS | A possible JavaScript engine for request-variable, header and filter subsets. Its runtime exposes memory, stack and interrupt limits. A scoped Cauce adapter would still need to reproduce njs request objects and bound each host callback. |
| Wasmtime | A possible Cauce-native typed extension interface. Fuel and epoch interruption cover guest execution, with separate resource limits and deadlines for blocking host operations. Adapter behavior would be measured independently of njs. |
| Compiled expressions | A narrower route for map and policy expressions over typed request metadata. Config-time validation can bound expression size and audit every available operation. |

The initial boundary would expose only allowlisted request fields and
typed mutations through existing header and response rules. Keep
filesystem, network, environment access and secrets outside that
interface. Limit memory, work and concurrency per invocation. Require
deterministic errors and independent resource-exhaustion tests. Keep
runtime and dependencies disabled by default until their behavior
and binary costs are measured.

[QuickJS documentation](https://bellard.org/quickjs/quickjs.html)
[Wasmtime interruption](https://docs.wasmtime.dev/examples-interrupting-wasm.html)
[Pinned add-on backlog](../compatibility/nginx-gap-backlog.jsonl).
