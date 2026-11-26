# Disk-backed HTTP cache

Add `disk.directory` and `disk.max_size` under a route's `cache`.
The configured directory is dedicated to one route and must be an absolute,
private, service-owned path under operator-controlled parent directories.
The cache creates the final directory when its parent exists. A process
holds one exclusive lock per configured directory.

The memory cache remains the first tier. After memory eviction, the lookup
can restore an eligible representation directly from disk. Both tiers use
the same method, authority, scheme, URI, Vary, authorization, freshness,
and stale policies. The existing `max_entry_size` applies to the body.
Each file also has bounded metadata and a SHA-256 checksum. Cauce counts
both an existing object and its staged replacement against the zone quota.
Up to 16 disk zones may total no more than 64 GiB in one configuration.

Disk work runs on bounded blocking workers. Completed bodies are eligible
for persistence. Files are synced before atomic rename. Generation and
invalidation fences prevent obsolete updates from being published in the
current process. Readers hold independent in-memory bytes during delivery.
The disk tier can miss under I/O saturation or storage errors, leaving
normal origin forwarding and the memory cache available.

A clean shutdown records the configuration identity and syncs the cache
directory. Startup validates the marker, file type, metadata, checksum,
age and quotas. A changed configuration or unclean shutdown starts cold.
This prevents reuse of stale data when a crash interrupts invalidation.
Unexpected or partial entries with Cauce object names are removed during
reconstruction. Disk cache configuration changes require restart.

The socket tests cover cold and warm disk access, memory eviction, disk
quota, corruption, mutation invalidation and interrupted-process recovery.
Filesystem ENOSPC and production performance remain qualification gates.
[Fixture](../tests/http_disk_cache.py).
