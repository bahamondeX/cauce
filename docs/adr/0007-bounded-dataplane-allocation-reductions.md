# 0007. Remove pool key copies and reuse JSONL writer buffers

**Accepted for deterministic work reduction.** Assign each logical endpoint a
numeric index during configuration compilation. Pool keys combine the index
with the immutable generation ID. Independent endpoint definitions preserve
their identity even if they share a network address or TLS policy. The
external connection ID keeps its `cN` format.

Count idle HTTP/1.1 sockets under the pool mutex. Update the count at return,
pop and reclaim, then reconcile it during expiry and generation sweeps.
Ordinary HTTP/1.1 return avoids walking every endpoint's idle list. Reclaim
still searches for a victim under pressure. Shared HTTP/2 admission and expiry
retain the existing physical semaphores and stream lifetime guards.

The JSONL writer owns one retained 128-event batch vector and one growable
serialization buffer. Clear the buffer before writing each record. Queue
bounds, JSON encoding, batch size, per-batch flush and rotation behavior
follow the existing collector contract. The buffers retain their high-water
capacity for the writer's lifetime. Long event fields remain bounded by the
existing request and configuration limits.

The socket and collector suites exercise reuse, rotation, ENOSPC, interrupted
tail handoff and checkpoint recovery. Source inspection proves the removed
key copies, count scans and per-record serialization buffer initialization.
Deployment throughput and CPU-per-request effects await qualified comparisons.

[Milestone 2B results](../PERFORMANCE-MILESTONE-2B.md).
