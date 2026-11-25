# Reference fixtures

The pinned fixtures serve cleartext HTTP/1.1 echo traffic to the same Node
backend. They are isolated from the Cauce runtime and dependency graph.
Use Linux Docker with at least three allocated CPUs for the smoke runner.

```sh
curl -fsSLO https://www.haproxy.org/download/3.4/src/haproxy-3.4.8.tar.gz
sh bench/program/references/build-haproxy.sh haproxy-3.4.8.tar.gz /tmp/haproxy-ref
docker build -t cauce-pingora-reference:0.9.1 bench/program/references/pingora
docker build -t cauce-increment7-backend:local bench/increment7/backend
docker pull python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740
docker pull debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
sh bench/program/references/smoke.sh /path/to/linux/cauce-load /tmp/haproxy-ref/haproxy /tmp/ref-smoke
```

Pingora admits bodyless GET requests, applies a 32-request semaphore and
sets the upstream idle pool to 32. HAProxy uses a one-thread frontend and
a 32-connection server limit. Their idle sockets, overload behavior, request
filters and deadlines differ from Cauce. The smoke runner checks protocol,
byte integrity and offer accounting. TLS, gRPC and application access logs
are outside the smoke workloads. A capacity comparison needs equivalent
physical connection and admission behavior under saturation before the
first measured cell.

The fixtures use [Pingora's pinned Apache-2.0 source](https://github.com/cloudflare/pingora/tree/f86ffda5d3ae5c3baf5125682138b33b26c01b08)
and [HAProxy's upstream source release](https://www.haproxy.org/download/3.4/src/haproxy-3.4.8.tar.gz).


Pass `--admission` to `smoke.sh` to hold 32 concurrent backend requests and
observe the excess request and recovery. The Pingora fixture responds 503
at its application permit boundary. The HAProxy fixture queues behind its
32-connection server limit. This characterization records the overload
policy difference before interpreting any matched-rate measurements.
