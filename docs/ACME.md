# Automatic certificate renewal

The Edge and Full builds support `proxy.acme` through instant-acme.
Supply the ACME directory URL, accepted terms, a private absolute state
directory and one or more lists of DNS names. Each list becomes one
certificate order. HTTP-01 requires a cleartext HTTP listener reachable
by the CA. The ordinary URL authority must match the authorized hostname.

Configure HTTPS and QUIC with a bootstrap certificate. Both use the same
live SNI resolver after issuance. When multiple usable groups cover a
name, configuration order selects the first group. Manual certificates
provide fallback until issuance and after expiry. Existing transport
connections continue through certificate rotation. A custom directory CA
can be supplied for a private ACME server such as Pebble.

Account and certificate files are owner-only. Each update uses a
temporary private file, fsync and atomic rename. The manager acquires an
exclusive state-directory lock, verifies key and certificate consistency,
checks hostname coverage and tracks expiration. Recovery backs off after
failed renewal. The admin metrics endpoint exposes
`acme_next_expiration_timestamp_seconds`, and certificate publication
events report the group index and actual expiration.

The Pebble v2.9.0 fixture uses real HTTP-01 and a local DNS server. It
issues a two-name certificate, rotates it while an HTTPS connection stays
open, verifies the active certificate on QUIC, and checks restart recovery.
DNS-01 provider integration and public-CA interoperability remain open.

[Configuration fixture](../tests/acme_pebble.py)
[instant-acme](https://docs.rs/instant-acme/0.8.5/instant_acme/)
[Pebble](https://github.com/letsencrypt/pebble/tree/v2.9.0).
