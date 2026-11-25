# Collector observed-frontier regression

## Reproduced defect

The unmodified 0.9.1 snapshot could silently accept loss of already observed,
unread bytes when a log shrank without changing inode. It checked size against
the committed offset, but not the larger observed frontier. This affected both
a full spool and scanning after capacity became available. An unchanged committed
prefix passed the content-anchor check. Inode reuse after unlink/recreate could
expose the same issue depending on filesystem allocation.

Two deterministic tests truncate the same file to its committed prefix after a
second, unread event has been observed. Both **failed before the fix**; see
[before](collector-frontier-before.log). These tests do not rely on inode reuse,
thread timing, or probabilistic filesystem behavior.

## Fix

Both normal scanning and full-spool frontier tracking reject a file size smaller
than its saved observed frontier, before changing the checkpoint or spool.
Tests verify that rejected scans retain the original observed size and pending
bytes. There is no schema migration, ACK change, credential change, or automatic
loss recovery. Lost bytes cannot be reconstructed: fail-closed reporting is the
intended outcome.

The existing missing-generation test now keeps the unlinked old file open so its
inode cannot be reused; this makes that test specifically exercise the distinct
missing-generation confirmation window. The new tests cover same-inode shrink.

## Verification

- Formatting and Clippy with warnings denied passed.
- 22 proxy tests and 13 collector tests passed: [after](collector-frontier-after.log).
- A separate image `cauce:0.9.1-frontier-fix-arm64` was built without replacing the
  validated 0.9.1 integration image: [build](collector-frontier-build.log).
- The fixed collector passed all eight local real-adapter integration cases:
  [report](connectivity-frontier-fix/report.json). This includes lost-ACK replay,
  authorization denial, precommit mutation and recovery. Connectivity source
  hashes were unchanged; existing containers were preserved and lab resources
  cleaned up. AWS is simulated by Moto, not a cloud deployment.

Reproduce from this branch:

```sh
docker build --platform linux/arm64 --no-cache-filter checks --target checks \
  -f docs/increment9-check.Dockerfile .
docker build --platform linux/arm64 -t cauce:0.9.1-frontier-fix-arm64 .
python3 scripts/test-connectivity-real-docker.py \
  --reference /path/to/connectivity-ingest-worktree \
  --image cauce:0.9.1-frontier-fix-arm64 \
  --report-dir work/frontier-fix-new-run
```

This isolated branch is not merged into the original Cauce tree or the separate
active-health branch. No Connectivity files or running deployment were modified.
The fix does not claim to detect every adversarial truncate-and-regrow operation
between observations; append-only logs and retention remain required.

Confirmation rerun with `--no-cache-filter checks`: formatting, Clippy, all 22
proxy tests and all 13 collector tests passed again. See
[confirmation](collector-frontier-confirm.log). No failed test was skipped.
