# Bundled SQLite remediation

The collector in the Milestone 9 checkpoint linked SQLite 3.50.2 through
`libsqlite3-sys` 0.35.0. RUSTSEC-2026-0459, also identified as CVE-2026-119375,
affects bundled SQLite releases before 3.54.0 when opening a database with
a sibling WAL of a different page size. SQLite 3.54.0 is the first patched
release. The upstream advisory maps it to `libsqlite3-sys` 0.39.0. [RustSec advisory](https://rustsec.org/advisories/RUSTSEC-2026-0459.html) [SQLite 3.54.0 release](https://sqlite.org/releaselog/3_54_0.html).

Cauce now locks `rusqlite` 0.41.1 and `libsqlite3-sys` 0.39.1. The collector
reports linked SQLite **3.54.1** through `cauce-collector --sqlite-version`.
The collector keeps checked unsigned bindings with rusqlite's `fallible_uint`
feature. Native and container builds use Rust 1.89 or newer.

The collector opens its local spool and enforces `DELETE` journaling. The
advisory's exposure requires file-level control of the database and sibling
WAL at open time. The configured spool and its parent directory remain a
local trust boundary. The update preserves the existing schema, journal mode,
durability settings and Connectivity delivery contract.

On Darwin ARM64, 23 collector integration cases passed against a synthetic
HTTPS sink, including stable batch identity across SIGKILL and restart. All 13
storage cases passed, including rollback at the configured SQLite page limit.
A separate 16 MiB disposable filesystem reached OS ENOSPC. The collector
preserved the existing database checkpoint and spool after the failed commit.
After space returned, the same database recovered 65 events and passed
`PRAGMA integrity_check`. The failure on this filesystem surfaced as
`SQLITE_CANTOPEN` while starting SQLite's journal work.

The RustSec scan used advisory database commit
`c9f6c91a44dbad9d71742329e9df061279f9c4be` and returned zero vulnerabilities
and zero warnings for the updated Cargo lock. The test sink establishes the
collector-side contract locally.

Milestone 11 kept the dependency lock and SBOM unchanged. The rebuilt
collector reported SQLite 3.54.1. It reopened the prior collector's
database and preserved its checkpoint on ENOSPC. Once space returned,
the fixture recovered 65 events and passed the integrity check.
[Milestone 11 preservation evidence](../bench/program/evidence/milestone-11/sqlite-preservation.json).

[Collector verification and raw scan](../bench/program/evidence/milestone-10/)
[Updated CycloneDX SBOM](../third-party/cauce.cdx.json).
