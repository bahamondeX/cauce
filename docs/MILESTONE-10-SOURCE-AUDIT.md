# Milestone 10 source audit

The audited local range is `c1c6231..701bd3b`. The two existing
`.DS_Store` changes belong to the workspace. The pinned registry has 120
OSS modules, 754 directive registrations and 149 configure patterns. The
70 add-on registrations remain separate. All 53 recorded evidence hashes
verified, and the retained collector reported linked SQLite 3.54.1.

| Commit | Verified source and evidence | Limit |
| --- | --- | --- |
| `285e363` | `AGENTS.md` and `scripts/stage-native-release.sh` require repository-local paths. | The lockfile recovery investigation is closed. |
| `c5e0798` | `Cargo.lock` locks rusqlite 0.41.1 and libsqlite3-sys 0.39.1. `src/bin/cauce-collector.rs` exposes `--sqlite-version`. The evidence includes integrity, recovery and RustSec results. | Connectivity evidence uses a local HTTPS sink. |
| `b79667a` | `scripts/nginx-inventory.py` reads pinned command tables. `scripts/nginx-gap-audit.py` emits source lines, setters, contexts and stable IDs. `tests/compatibility/fixtures.py` supplies loopback tests to `scripts/nginx-compatibility.py`. | Most registry rows lack reviewed argument values, defaults and merge semantics. |
| `924f48a` | `Routes::compile` and `location_parents` in `src/routing.rs` implement scoped matching. `Config::inherit_location_policies` resolves selected fields at load. `http_preview` in the migration tool emits literal nested routes. | Flat policy fields are merged individually. Regex uses the bounded Rust subset. A literal child below a regex parent has a recorded difference. |
| `63aca62` | `UpstreamTls.send_sni` and the rustls connector preserve hostname verification with SNI disabled. | NGINX defaults SNI off. Cauce retains its previous default. |
| `6aa4c26` | `headers::Policy::apply_response` appends bounded literal fields at selected statuses. The schema and migration preview preserve that subset. `stage-zig-linux.sh` binds artifacts to a source tree. | The header policy has no runtime expression engine or append-style inheritance. Protection for sensitive and protocol-owned headers remains deliberate. |
| `ab2df74` | `tests/dns_upstream.py` and `tests/mail/test_mail.py` exercise IPv4-to-IPv6 switching while old work drains. | The new cases establish DNS retirement. Stream and mail health and retained-socket configuration have separate gaps. |
| `1555ee5` | The backlog reviews all 15 nginx-acme command registrations. | Pebble is Cauce-only evidence. There is no add-on differential run. |
| `60c2cad` | `tests/http_disk_cache.py` covers coalescing, eviction during reads and ENOSPC. The proof gate requires named, passing two-engine cases. | `verify_claim` compares the module and case names. It can accept one unrelated case while all criteria are asserted as strings. |
| `1f2b3bb` | Address-access fixtures cover nearest inherited CIDR admission, local replacement and invalid CIDR rejection. | Cauce has an allowlist. NGINX also has ordered rules, method scopes and access-handler composition. |
| `74f95d6` | `scripts/stage-oci-layout.py` verifies ELF inputs and the source, lock and Debian descriptors before packaging. | OCI digest validation does not exercise entrypoint, user, permissions or shutdown. |
| `4d93de4` | `artifact-identities.json` identifies the exact `6aa4c26` runtime inputs. The matrix covers six configuration profiles and Full/Core ELF and OCI identities. The harness observes readiness and process resources. | Linux AMD64 used emulation. Loopback figures do not establish native competitive performance. |
| `701bd3b` | The migration suite passed 14 tests against an explicit `CAUCE_TEST_BINARY`. | The migration preview still blocks unsupported behavior and requires review. |

## Accounting and implementation findings

- The entry gate operates independently of module completion. The
  all-registration check applies when promoting a whole module.
- None of the inventory mappings requested exact promotion. A green
  module fixture alone could not establish the required per-registration
  behaviors. The previous harness projected selected headers and
  sometimes excluded the body. Those results establish their stated
  claims and require scoped disclosure.
- Core rewrite and route selection already have typed outcomes and a
  ten-hop limit in `src/actions.rs` and `src/proxy.rs`. HTTP, HTTP/2
  and HTTP/3 share the policy path. Runtime expression support is
  limited to numeric captures in route actions. YAML environment
  expansion is a configuration-time operation.
- In pinned NGINX, `ngx_http_core_find_config_phase` denies an external
  request selected into an `internal` location before location rewrite.
  The setter accepts no arguments, while `ngx_http_core_merge_loc_conf`
  inherits the flag and defaults it off. The initial Cauce selection and
  typed rewrite outcomes supply the corresponding execution boundaries.
- Expanding scoped merges and expression evaluation should unlock more
  registrations than adding new handlers one by one. That is a dependency
  hypothesis. The dashboard counts only verified behavior.

[Prior compatibility report](../bench/program/evidence/milestone-10/differential-final.json)
[Prior coverage](../bench/program/evidence/milestone-10/coverage-after.json)
[Prior artifact identities](../bench/program/evidence/milestone-10/artifact-identities.json)
[Prior evidence hashes](../bench/program/evidence/milestone-10/SHA256SUMS).
