# Milestone 11 semantic evidence

The baseline is `701bd3b` and the runtime checkpoint is `0f62952`.
The fixed NGINX 1.30.8 OSS denominator is 754 directive registrations,
120 modules and 149 configure patterns. Add-ons contribute 70
separate registrations.

| OSS directive assessment | Before | After |
| --- | ---: | ---: |
| Verified equivalent | 0 | 1 |
| Partially implemented | 137 | 137 |
| Mapped but unimplemented | 2 | 2 |
| Unmapped | 615 | 614 |

`ngx_http_core_module.internal` passes the registration manifest's
seven request cases and three shared configuration rejections. The
corresponding module remains partial. The suite verifies defaults,
location context, inheritance, rewrite order, an internal rewrite
into the protected child, and a transition from public to private
after reload. The raw report retains both default 404 responses and
excludes the generated bodies from the access decision comparison.

The first campaign stopped short of the 25-registration target.
The reviewed candidate `add_header_inherit` also applies in HTTP,
server and conditional location contexts. The complete `return`
directive needs status, URL, expression and context handling beyond
the current direct body. NGINX's `etag` affects several content
handlers and uses metadata-based strong validators, while Cauce
retains weak static validators. These candidates remain open.

The five previously skipped integration cases use exact local PHP,
uWSGI/SCGI, and Postfix/Dovecot images with readiness checks. Their
results and image IDs are recorded with the artifacts. All five
passed against the current gateway. Both pool strategies passed
24 HTTP integration cases. SQLite remained at 3.54.1. The local
collector rerun passed 23 integration cases, 13 storage cases and
the dedicated ENOSPC recovery. The dependency lock and recorded
advisory report retain their previous identities.

## Build and loopback results

All six profiles built and passed configuration checks. The
`0f62952` executable pairs are available for Linux ARM64 and AMD64.
The ARM64 ELF smoke ran in the local Linux guest. AMD64 smoke used
user-mode emulation. Full and Core ARM64 images passed the separate
OCI runtime check.

| Linux architecture | Full gateway bytes | Core gateway bytes | Full OCI layer bytes | Core OCI layer bytes |
| --- | ---: | ---: | ---: | ---: |
| ARM64 | 13,917,088 | 10,221,016 | 37,367,243 | 35,686,857 |
| AMD64 | 15,835,856 | 11,754,608 | 38,017,555 | 36,281,916 |

The ARM64 Linux guest fixture returned identical bodies in every mode
over one persistent HTTP/1 connection per run. Each mode ran three
rounds of 2,048 requests after 64 warm-up requests. Median run p50s
were 79.3 μs for Full with a literal body, 79.8 μs for Full with an
evaluated body and 77.0 μs for Core with a literal body. Small differences
between modes are within the run-to-run variation of the fixture.
Both literal modes use prebuilt bytes. The expression mode builds
its bounded output. Allocation counts, native competitive workloads,
and multicore qualification remain open.

[Source audit](MILESTONE-10-SOURCE-AUDIT.md)
[Internal proof](../bench/program/evidence/milestone-11/internal-proof.json)
[Coverage before](../bench/program/evidence/milestone-11/coverage-before.json)
[Coverage after](../bench/program/evidence/milestone-11/coverage-after.json)
[OCI runtime verification](OCI-RUNTIME-VERIFICATION.md)
[Artifact identities](../bench/program/evidence/milestone-11/artifact-identities.json)
[Raw loopback samples](../bench/program/evidence/milestone-11/expressions-loopback.jsonl)
[SHA-256 manifest](../bench/program/evidence/milestone-11/SHA256SUMS).
