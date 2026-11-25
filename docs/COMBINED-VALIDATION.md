# Combined health + collector-frontier validation

Branch: `health-frontier-integration`. Both changes are combined only in this
isolated worktree. The original Cauce runtime/manifests remain byte-identical to
the initial snapshot (14 files; [verification](original-source-preserved.json)).

## Passed

- Formatting and Clippy with warnings denied.
- **39 Rust tests**: 26 proxy/health and 13 collector. No skipped failures.
  [Full check log](combined-checks.log).
- Separate ARM64 image `cauce:0.9.1-health-frontier-arm64`.
  [Build log](combined-build.log).
- **8/8 real-adapter local integration scenarios** using that image.
  [Machine-readable report](connectivity-health-frontier/report.json).
- Connectivity sources unchanged by hashes; preexisting containers preserved;
  temporary test resources removed; no tokens detected in test output.

The integration report records exact image and collector binary hashes. It uses
a source copy of the real Connectivity adapter/authenticator/sink with Moto for
AWS. No production deployment, real-cloud durability or complete dashboard/login
flow is implied. Health probes are opt-in HTTP/1 only; HTTP/2/gRPC health remains
unsupported. See [health behavior and limits](ACTIVE-HEALTH.md) and
[collector defect, before/after evidence and limits](COLLECTOR-FRONTIER-FIX.md).

## Reproduce

```sh
docker build --platform linux/arm64 --target checks \
  -f docs/increment9-check.Dockerfile .
docker build --platform linux/arm64 -t cauce:0.9.1-health-frontier-arm64 .
python3 scripts/test-connectivity-real-docker.py \
  --reference /path/to/connectivity-ingest-worktree \
  --image cauce:0.9.1-health-frontier-arm64 \
  --report-dir work/combined-new-run
```

No changes are promoted automatically to the original tree. This branch is a
validated candidate for the coordinator to promote after preserving concurrent
work. Existing images/services were not replaced.

## Local promotion

The 18 reviewed files were promoted to the original Cauce directory after a
fresh no-conflict check against the baseline. Original versions, checksums and
commit patches are preserved in the local promotion backup; full Git history
remains in the isolated repository. The original directory has no repository of
its own, so its parent home repository was not altered. Only selected files were
copied; running services and existing image tags were not replaced. The
source-preserved report above records the **pre-promotion** check, not a claim
that the promoted source is still the baseline. Post-promotion verification is
recorded separately in PROMOTION-VALIDATION.md.
