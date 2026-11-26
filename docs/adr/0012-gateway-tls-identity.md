# ADR 0012 · Gateway TLS identity and reload

Each prepared configuration owns its TLS credentials, trust bundles and
resumption state. The inbound rustls resolver maps exact DNS SNI names to
validated certificate chains and provides one explicit default chain. With
no SNI map the existing single-certificate resolver remains in use. Routing
still uses the validated HTTP authority. The listener may require a client
certificate trusted by its configured client CA. Each upstream retains its
own explicit origin identity and trust roots and may present a client
certificate. Versions and session resumption are set at the TLS boundary.

TLS preparation performs file parsing and name and key checks before
publishing any part of the candidate. Reload publishes the complete listener
policy, upstream client configurations and routing generation together.
New requests can use only pools for that generation. New downstream
handshakes use the new policy and cache. A connection completing an older
handshake retains its TLS identity until it closes or drains.

Per-SNI client trust, wildcard selector rules, OCSP and ACME are follow-up
capabilities. Application-level client certificate authorization is outside
the current listener policy. [Pinned rustls source](https://github.com/rustls/rustls/tree/v/0.23.46/rustls/src).
