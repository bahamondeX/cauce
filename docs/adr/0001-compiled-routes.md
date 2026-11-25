# 0001. Compile route lookup into each configuration generation

The implementation is in place. The release performance gate is pending.

The published route matcher walks the full route list for each request. Configurations can contain up to 1,024 routes. Exact authority and segment-aware longest prefix are already part of the request contract, including port significance and asymmetric matching of `/api` and `/api/`.

Compile a hash map keyed by the lowercase authority and a compressed byte radix tree per authority when constructing the validated generation. Terminal nodes store offsets into that generation's immutable route vector. Check the boundary at each matched terminal, retaining the deepest valid ancestor. Keep the request authority validation and ambiguous path rejection in the proxy handler. A one-route generation compares the route directly.

The new index adds memory and CPU cost to generation construction. It removes per-request route cloning and preserves the generation and endpoint scope of existing leases. Reload replaces the index as part of the same generation as its routes and upstreams. The focused semantic regression compares both matchers across overlapping prefixes, miss cases, authority ports, configurations with a single route, and several insertion orders.

See the [lookup results and release gates](../PERFORMANCE.md) for measured effects and limits.
