# 0003. Construct access events when JSONL logging is enabled

A symbolized Darwin profile of the previous implementation showed event UUID
generation and timestamp formatting during a workload with access logging
disabled. With the event queue absent, the proxy discarded every event.

The request handler checks the logger before building an access event. Both
pool protocols build per-attempt diagnostics only for a recorded request.
The request ID and forwarded trace context are part of proxy behavior. They
stay available in both logging modes. Pool timing for Prometheus, response
counts and bytes, and all admission and transfer lifetimes remain independent
of the access event.

Keep the existing enabled-mode JSONL contract, queue capacity and file
rotation. Verify the disabled-mode behavior with a real-socket regression
covering trace forwarding, distinct request IDs, pool reuse, rejection and
response counters, and filesystem output. Run the paired release-build
comparison in both logging modes. [Method and results](../PERFORMANCE.md).
