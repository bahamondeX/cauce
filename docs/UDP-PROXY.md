# UDP proxy

Configure `proxy.stream.udp` and a group of literal IP socket addresses.
An IPv4 or IPv6 listener owns a table keyed by client address and port.
The selected upstream and connected UDP socket live for the duration
of the association. A connected socket filters replies by upstream
address and port. Each direction preserves datagram boundaries.

`max_sessions` is global to the listener and
`max_sessions_per_ip` shares a cap across the client's ports.
A full session queue drops new input. Each association has a four-packet
queue and one receive buffer. An extra byte in the receive buffer makes
over-limit truncated datagrams distinguishable from valid maximum-sized
ones. `idle_timeout` expires inactivity after the last accepted upstream
send and `session_timeout` caps total lifetime. Socket-level upstream
unreachable errors trigger a bounded cooldown. Silent packet loss leaves
the association in place until expiration.

Each forwarded input grants at most one response packet and up to three
times the input payload in response-byte credit, with one byte charged
for empty datagrams. Credits are shared within an association and capped
at 64 packets. Deploy on a network with source validation and size the
`max_response_ratio` to the service. Large request-response asymmetry
may require TCP fallback or a protocol-aware frontend.

On SIGTERM the listener stops accepting datagrams and closes session
queues to new input. Existing sessions can flush queued requests and
outstanding replies within the shutdown deadline. UDP metrics expose
active, admitted and rejected sessions, upstream errors and drops.

`reuse_port` uses kernel socket selection across processes. Each process
owns its session table and capacity. The runner compares single-process
behavior with generic NGINX Stream UDP. Generic HAProxy OSS UDP
forwarding requires independent capability verification.
