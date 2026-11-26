# Listener lifecycle

HTTP and QUIC retain the original bound sockets across SIGHUP. Reload
prepares routes, upstream TLS, HTTP listener certificates and QUIC
configuration before publishing. Established connections retain their
transport state.

Stream TCP, TLS passthrough, UDP and mail support adding and removing
non-overlapping bindings. Preparation validates all additions and binds
their sockets before publishing the generation. A bind conflict drops
prepared candidate sockets and leaves the active service operating.
Unchanged bindings keep their original listener task and runtime policy.

Removal stops admitting new TCP and mail connections. Established TCP
and authenticated mail sessions drain with their existing deadlines.
Removed UDP listeners admit no new association and continue serving
existing peers until idle or the session deadline. Cauce permits at most
one draining listener topology during reconfiguration. SIGTERM stops
admission and drains within the configured global shutdown deadline.

Overlapping sockets, changes to a retained Stream or mail binding,
Stream upstream group changes and Stream/mail PEM rotation still require
restart. HTTP/QUIC listener topology, process limits and disk-cache
configurations also require restart.

[Stream TLS settings](TCP-PROXY.md#stream-tls)
[Reload configuration](CONFIGURATION.md#reload)
[Stream rotation fixture](../tests/listener_reload.py)
[Mail fixture](../tests/mail/test_mail.py).
