# NGINX compatibility tooling

The development CLI uses Python 3.11 or newer and its standard library.
From the Cauce root, supply the pinned, checksummed archives and an NGINX
OSS 1.30.8 executable with the fixture's modules enabled.

```sh
python3 -IB "$PWD/scripts/nginx-compatibility.py" audit \
  --tarball work/program/nginx-1.30.8.tar.gz \
  --njs-tarball work/program/njs-1.0.2.tar.gz \
  --acme-tarball work/program/nginx-acme-0.4.2.tar.gz
python3 -IB "$PWD/scripts/nginx-compatibility.py" report --format json
python3 -IB "$PWD/scripts/nginx-compatibility.py" queue --module ngx_http_proxy_module
python3 -IB "$PWD/scripts/nginx-compatibility.py" test \
  --module ngx_http_proxy_module --nginx "$PWD/work/nginx/objs/nginx" \
  --cauce "$PWD/target/debug/cauce"
```

`queue` emits stable registration identifiers and available fixture selectors.
Each registration has a source file, exact line, command table and parser
arity. A reviewed subset also records the directive vocabulary, defaults and
inheritance. Fields requiring manual semantics review are explicitly `null`
and the audit state is `pending`. Configure flags remain build-scope rows.

`test` starts both processes on independent loopback listeners with generated
configuration, then records status, exact body bytes and the ordered values of
each selected repeated header. The report lists header fields excluded from
comparison. Those exclusions include process identity and other fields outside
the particular fixture's assertions. Exit status, forced shutdown, RSS and
process CPU-time snapshots accompany results. Resources are diagnostic in the
current fixture and do not establish capacity or performance parity.

Fixture results qualify only the assertions and configuration interactions
they exercise. The registry requires separate evidence for full arguments,
contexts, defaults, inheritance, failures and interaction coverage.

The machine-readable assessment distinguishes unmapped, mapped but
unimplemented, partial, implemented but unverified, verified equivalent
and platform or build scope. Milestone 11 requires a registration-specific
manifest. The verification report binds the pinned source, both binaries,
the Cauce revision, and the committed harness and fixture. The audit
recomputes normalized comparisons from the raw responses and verifies
every required configuration, behavior and lifecycle case.

```sh
registration=oss/src/http/ngx_http_core_module.c/ngx_http_core_module/internal/29
python3 -IB "$PWD/scripts/nginx-compatibility.py" manifest "$registration"
python3 -IB "$PWD/scripts/nginx-compatibility.py" test \
  --registration "$registration" --source "$(git rev-parse HEAD)" \
  --nginx "$PWD/work/nginx/objs/nginx" --cauce "$PWD/target/debug/cauce"
```

Use the full Git hash of the runtime used to build `--cauce`. The
current HEAD is the expected source only when the selected executable
was built from it.

Migration and JSON Schema fixtures use a repository-local lock. From the
Cauce root, select an installed Python 3.11–3.14 interpreter explicitly.

```sh
mkdir -p "$PWD/work/compat-python/tmp"
export UV_CACHE_DIR="$PWD/work/compat-python/cache"
export UV_PROJECT_ENVIRONMENT="$PWD/work/compat-python/venv"
export UV_PYTHON_INSTALL_DIR="$PWD/work/compat-python/interpreters"
export UV_PYTHON_DOWNLOADS=never TMPDIR="$PWD/work/compat-python/tmp"
uv sync --locked --project "$PWD/tests/compatibility" --python "$(command -v python3)"
export CAUCE_TEST_BINARY="$PWD/target/debug/cauce"
"$UV_PROJECT_ENVIRONMENT/bin/python" -IB -m unittest discover \
  -s "$PWD/tests" -p test_nginx_migration.py -v
```

Set `CAUCE_TEST_BINARY` to the freshly built executable under test. This
also enables runtime validation of generated configurations where applicable.
Select `--extra quic` for local Pebble and HTTP/3 fixtures,
`--extra content` for the independent Brotli decoder, or `--extra grpc`
for the standard gRPC health service.
