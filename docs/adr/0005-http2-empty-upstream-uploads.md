# 0005. End empty upstream HTTP/2 uploads in the initial headers

The checkpoint's HTTP/2 stacks include the upload body path. It wrapped
even already-ended requests in a guarded body before handing them to Hyper.
The guard allocated its idle timer and reported a live body until polled.

For an HTTP/2 upstream, forward a known-ended incoming body as an empty
body. Use Hyper's end-of-stream contract, which accounts for pending DATA
and trailers. An upload with pending frames still owns its upload guard and
request slot. The response lease and absolute stream deadline continue to
cover the upstream exchange. HTTP/1.1 upstreams use their existing framing.

Retain the change for the verified reduction of one framing operation and
one unused upload timer per known-ended request. The Node server saw the
`END_STREAM` flag on initial HEADERS over both cleartext and TLS. The
checkpoint failed that assertions path. The candidate forwarded zero-data
request trailers and passed the gRPC and HTTP/2 lifecycle suites. The
paired Colima CPU and latency intervals span zero. The decision carries
no end-to-end CPU or latency improvement claim.

[Protocol tests](../../bench/program/evidence/milestone-2/http2-eos-candidate.json)
[Experiment results](../PERFORMANCE-MILESTONE-2.md#http2-empty-upload-experiment).
