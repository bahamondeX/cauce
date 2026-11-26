# ADR 0017. TLS-gated mail listeners

Status: accepted for the Milestone 8 functional checkpoint.

Mail binds independent TCP sockets. Each session negotiates SMTP, IMAP or POP3 under its own deadline, validates credentials through an explicitly configured HTTP service, and logs into a backend selected from a fixed per-listener allowlist. The same socket carries the authenticated session. Cauce reuses rustls certificate preparation and the TCP relay with half-close and bounded backpressure.

The plaintext negotiation stage permits capabilities and TLS upgrade. STARTTLS rejects prefetched plaintext and resets the SMTP greeting state. Cauce enables authenticated SMTP submission. The backend controls post-authentication commands and message delivery. Credential headers are sensitive and metrics are aggregate. HTTP authentication is permitted on loopback and HTTPS is verified elsewhere. Backend TLS is required outside loopback.

The first release requires a process restart for mail listener and certificate changes. HTTP and QUIC publication retain their existing generation boundaries. The default distribution includes mail and prepares backend/authentication clients only when mail is configured.
