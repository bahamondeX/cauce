# Local promotion verified

The reviewed health and collector-frontier changes are now in the original
Cauce directory. All 21 changed source/document files match the reviewed
candidate after documentation updates; see [hashes](promotion-source-verification.json).
The original directory has no independent Git repository. Its parent home Git
was untouched; full commits remain in the isolated integration repository and
commit patches plus pre-change copies are retained in the local promotion backup.

## Post-promotion verification

- `cargo fmt --check` passed in the original directory.
- Docker checks were rerun from the original directory with `--no-cache-filter checks`.
  Formatting, Clippy with warnings denied, **26 proxy/health + 13 collector tests**
  passed. [Check log](promoted-checks.log).
- **8/8 local real-adapter integration scenarios** passed using the separate image
  built from matching candidate sources. [Report](connectivity-promoted/report.json).
- Connectivity source hashes unchanged; existing services preserved; lab resources
  removed; no production deployment, service restart or replacement of old image tags.

Backups and commit patches are in `work/promotion-health-frontier-b21faab`.
The manifest records original and candidate hashes; `before` holds originals,
`doc-update` holds pre-documentation-update versions, and `history` contains
commit patches. Avoid blindly restoring over subsequent work.

Remaining scope is unchanged: mTLS/ACME, HTTP/2/gRPC health, cloud deployment,
real login and full dashboard integration are not claimed complete. AWS in the
integration test is simulated by Moto.
