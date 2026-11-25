# 0004. Invalidate the application package in shared Docker build caches

The Dockerfile mounts one persistent Cargo target directory per architecture.
A telemetry reference restored from an older source archive produced the
checkpoint image ID. BuildKit saw different `COPY src` contents, but Cargo
reused application outputs because the archived files predated its freshness
markers. That image is rejected as a reference.

Clean the Cauce package for the target release profile before an uncached
Dockerfile build step. Keep the registry and dependency caches. The BuildKit
step can still be reused for identical source inputs. Repeat verification by
building the checkpoint and distinct source snapshots through the same
Dockerfile procedure and comparing exported binary hashes and image IDs.
Preserve the stale-build log with the accepted build identities.
