# ADR 0008 · Endpoint-sharded pool experiment

**Status** Experimental. Compact remains the production default.

## Context

The Milestone 2B synthetic model favored endpoint sharding at 64 endpoints
and favored the compact layout at one endpoint. The model omitted connection
drivers, actual leases, and HTTP/2 stream admission. [Milestone 2B comparison](../PERFORMANCE-MILESTONE-2B.md).

## Decision

Expose a process-start test selector, `CAUCE_POOL_STRATEGY=compact|sharded`.
The pool and every prepared configuration generation use the same strategy
for the life of the process. TLS identity still belongs to the compiled
endpoint and generation. YAML selection and a measured automatic threshold
remain open.

In sharded mode a compiled numeric endpoint ordinal selects one mutex.
An HTTP/1.1 idle list supports constant-time LIFO return and retrieval in
the common case. Expired or closed entries are skipped. A separate
global and endpoint idle semaphore accounts for every idle connection,
independently of the physical connection permits. Owned permits follow
the socket or its HTTP/2 idle entry. The last HTTP/2 stream publishes its
idle credit under the endpoint lock. Admission removes that credit under
the same lock. A full idle budget retires a newly idle socket.

Under global physical capacity pressure the pool checks a rotating range
of used buckets and removes at most one eligible idle connection in each
scan. It holds one endpoint mutex at a time. The rotation disperses
victims. There is still one process-wide wake-up mechanism, one global
connection and idle budget, and a bounded scan in the pressure path.
The prototype keeps one per-endpoint HTTP/2 list. A future available-stream
index needs evidence that saturated-list scans matter.

## Evidence and promotion

The Colima socket matrix accounted for every request at 1, 4, 16, 64, 256 and
1,024 logical endpoints with both strategies. At 64 logical HTTP/1.1
endpoints, lock-hold p99 was lower and lock-wait p99 was higher with
sharding. The test ran at 128 offers/s with tracing. It exercises endpoint
identity and churn through real sockets to a shared loopback backend.
It leaves throughput, fairness under saturation, per-strategy memory,
and the selection threshold open.
[Socket matrix](../../bench/program/evidence/milestone-2c/pool-matrix.json).

Keep compact until qualified native measurements show a practical
benefit and no regression in quotas, tail latency, fairness, reuse or
resource efficiency. Compare both strategies using the same binary.
[Milestone 2C qualification plan](../PERFORMANCE-MILESTONE-2C.md).

## Milestone 3 eviction correction

A Linux-independent real-socket burst reproduced five generation-B
connection IDs while a generation-A lease held one of four physical permits.
Two returned generation-B sockets were retired during global pressure
scans and replaced. All 98 requests completed. A sharded pressure scan now
protects the exact requesting endpoint and generation while looking for
victims. This leaves a concurrent same-key return available to waiting
requests and still permits eviction of other endpoint or generation keys.
A deterministic socket test checks both upstream protocols. Three traced
burst runs preserved the reuse bound. Compact remains the default.
[Before and after traces](../../bench/program/evidence/milestone-3/sharded-reuse-race.tar.gz).
