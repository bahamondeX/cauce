# Listener lifecycle

HTTP and QUIC retain the original bound sockets across SIGHUP. A reload
prepares routes, upstream TLS, HTTP listener certificates and QUIC server
configuration before publishing. Established connections retain their
transport state. A bind address, listener topology, admission setting or
transport limit that changes requires process restart.

Stream and mail listeners retain their process-owned TCP or UDP sockets.
Stream TCP accepts optional TLS termination through the same certificate
and trust policy used elsewhere in Cauce. Stream and mail topology and
certificate changes currently require restart. New TLS material is
validated before the configured sockets are opened. SIGTERM stops
accepting new sessions and drains existing sessions within the global
shutdown deadline.

[Stream TLS settings](TCP-PROXY.md#stream-tls)
[Reload configuration](CONFIGURATION.md#reload).
