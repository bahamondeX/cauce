# ADR 0016 · Exclusive application upstream leases

The configuration compiles each application route and selects a typed
upstream transport. Routing, request admission, response filtering,
cache publication, compression and access logging remain common.
HTTP/1.1 retains its Hyper sender and driver. HTTP/2 retains its
connection and stream accounting. Application transports lease a
byte socket through the existing global and per-endpoint pool budgets.

A FastCGI response owns the socket while its framed data is read.
The lease receives the socket back only after protocol completion.
The endpoint and generation identify the idle socket and prevent
reuse across incompatible configuration or security contexts. The
application response path runs only on application routes and shares
the existing response body guards. Fixed script mapping lets the
gateway compile the script before traffic and avoids path expansion
from HTTP input.
