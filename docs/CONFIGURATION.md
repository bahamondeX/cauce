# Proxy configuration

Cauce reads version 1 YAML from paths ending in `.yaml` or `.yml`. An explicit TOML path still uses the legacy layout. With no path, the native command uses `examples/cauce.yaml`. The current container and Compose examples continue passing their TOML paths explicitly.

```sh
cargo run --locked -- check examples/cauce.yaml
cargo run --locked -- examples/cauce.yaml
```

YAML starts with `version: 1`. Existing proxy fields go below `proxy`. Unknown fields, repeated mapping keys, extra documents and custom tags are rejected. The sample includes a YAML language server modeline for the [editor schema](../schemas/cauce.schema.json). Cauce also validates authority syntax, referential integrity, compatible limits, and configured TLS material at runtime. [Complete YAML example](../examples/cauce.yaml).

Duration names retain their units for interoperability with TOML. An integer in `headers_ms` means milliseconds, and an integer in `tcp_keepalive_secs` means seconds. The duration fields also accept whole-number values with `ms`, `s`, `m` or `h` suffixes. Conversion must fit the field and satisfy its configured limits. For example, `headers_ms: 5s` and `headers_ms: 5000` are equivalent.

Byte fields accept integer bytes or whole-number values with `B`, `KiB`,
`MiB` or `GiB` in YAML and TOML. The suffixes are binary units. For example,
`max_header_bytes: 32KiB` is 32,768 bytes. These units apply to header and
body limits, JSONL rotation size, and HTTP/2 window and send-buffer bytes.
Configuration preparation checks conversion and applies the field's
existing limits to the resulting integer.

Use an entire YAML scalar such as `"${CAUCE_SERVER_KEY_FILE}"` to load a value from the process environment. Substitution runs on parsed scalar values and never reparses variable contents as YAML. References work with string fields and unit-suffixed durations and sizes. An unset or non-Unicode reference rejects the candidate. The aggregate expansion budget is 1 MiB. YAML substitution and validation diagnostics omit resolved values. TLS PEM errors identify the failing operation. Use file paths for TLS private keys. Names and upstream or route IDs are operational labels that can appear in logs.

## Virtual hosts and locations

Use a lowercase exact authority, a leading wildcard such as `"*.example.test"`,
or a trailing wildcard such as `"mail.*"`. A wildcard consumes one or more
complete DNS labels. Supply the port in the pattern when the client sends it
in the authority. For example, `"*.example.test:8080"` matches
`tenant.example.test:8080`. Matching selects the exact authority first, then
the longest leading wildcard, then the longest trailing wildcard.

Choose `path_match: exact` for a literal path. The match ignores the query.
Set `path_prefix` to the path in either mode. Exact paths take priority within
the selected host. The default mode uses the longest segment-bounded prefix.
A host with no matching path returns 404. Match selection never falls through
to another virtual host. [Complete YAML example](../examples/wildcard-hosts.yaml).

## Reload and rollback

Prepare the full candidate in a file next to the active configuration, retaining the same YAML or TOML extension. Run `cauce check` against the candidate, rename it over the active path on the same filesystem, and send the running process SIGHUP. The running process checks the listener, limits, HTTP/2 and logging settings for restart requirements and prepares certificates, trust stores, routes and upstreams before activating a generation. A rejected candidate leaves the current generation active.

Keep the last known valid file. To roll back an accepted change, atomically republish that file and signal SIGHUP again. A successful rollback creates a new generation. Existing streams complete with the leases they acquired before the reload. The status log records `reloaded` or `reload_rejected`.

To migrate manually from TOML, preserve the proxy values in a version 1 YAML file and validate it before swapping the process configuration path. To assess migration from NGINX, use the [pinned compatibility registry](../compatibility/nginx-oss.yaml). Its directive status describes the degree of Cauce overlap, including documented semantic differences.

## NGINX migration report

Run the offline report on an NGINX configuration:

```sh
uv run --no-project --with pyyaml python scripts/nginx-migration-report.py nginx.conf
uv run --no-project --with pyyaml python scripts/nginx-migration-report.py nginx.conf --preview candidate.yaml
cargo run --locked -- check candidate.yaml
```

The report identifies each directive's line, context, reference registrations, coverage and known semantic differences. The registry includes the pinned NGINX source release, njs HTTP and stream modules, and the nginx-acme module and its issuer-block directives. Analyze included files separately. Directive arguments stay out of the report. The preview accepts a single cleartext HTTP server with an explicit IPv4 listener, explicit DNS names or edge wildcards, and literal exact or plain prefix locations that proxy to literal IPv4 upstreams. It generates a review-only file with Cauce defaults and records its confidence and a validation command. Review route matching, host forwarding, buffering, timeouts and capacity before use. Unsupported structures cause the preview command to exit unsuccessfully, and an existing output file is protected.

Regenerate the source inventory with `python3 scripts/nginx-inventory.py` or verify the generated file with `--check`. Pass `--tarball`, `--njs-tarball` and `--acme-tarball` to use local copies of the checksummed sources. Maintain Cauce feature statuses and test references in [nginx-coverage.json](../compatibility/nginx-coverage.json).
