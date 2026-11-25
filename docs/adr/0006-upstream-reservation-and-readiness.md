# 0006. Synchronize upstream readiness and connection capacity release

**Accepted for correctness.** The HTTP/1.1 response body can finish on one
Tokio worker before its Hyper connection driver polls the dispatch channel
again on another worker. A pool checkout promises exclusive ownership of
the sender. Readiness still belongs to the driver. Await the sender's
readiness within the existing request header deadline before handing it the
request. The request is sent once.

Bind the physical endpoint and global permits into a drop guard as soon as
they are both reserved. Cancellation and connect gate expiry release them
and wake waiters. The driver retains a guard reference for socket lifetime.
The HTTP/1.1 lease retains its reference through the existing upgrade path.
Permit release follows transport cleanup. A finished HTTP/1.1 lease wakes
waiters when it returns a usable socket. A discarded lease leaves wake-up
to the physical permit guard. A transport that finishes after a reload can
remain in use by an existing response. Publishing it to the idle pool
checks the generation under the same lock as the sweep.

An HTTP/2 opener checks every eligible sender for a free stream after it
acquires the opening gate. This closes a cold-open path that could overlook
capacity on a later connection when the first sender was full.

The paused-driver regression shows a completed first response, an exclusive
second lease and a driver whose dispatch channel is still busy. Removing
the readiness await makes that second send fail. A separate test cancels
an opening for each protocol with both physical permits held and observes
the notification and returned permits. The real-socket tests cover concurrent
reuse, reload with an active old lease, cross-generation eviction and backend
disconnects under one, two and four workers.

[Incident status and regression evidence](../PERFORMANCE-MILESTONE-2B.md).
