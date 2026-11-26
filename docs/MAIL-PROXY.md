# Mail proxy

Use `proxy.mail` to declare SMTP submission, IMAP and POP3 TCP listeners in YAML. Use `[[mail]]` in TOML. Each listener configures its TLS certificate and key, allowed authentication mechanisms, an authentication URL, and an allowlist of literal backend sockets. See the [YAML](../examples/mail.yaml) and [TOML](../examples/mail.toml) examples. Supply the PEM paths before running `cauce check`.

| Protocol | Before authentication | Authentication | Origin negotiation |
| --- | --- | --- | --- |
| SMTP | EHLO, HELO, STARTTLS, NOOP, RSET, QUIT | SASL PLAIN and LOGIN with TLS and EHLO | Greeting, EHLO and AUTH PLAIN |
| IMAP | Tagged CAPABILITY, STARTTLS, NOOP and LOGOUT | LOGIN and SASL PLAIN, with quotes and escapes in LOGIN | Greeting and tagged LOGIN |
| POP3 | CAPA, STLS and QUIT | USER and PASS with TLS | Greeting and USER/PASS |

Set `tls_mode` to `starttls` for explicit upgrade or `implicit` for TLS from the first byte. The shared rustls loader supports default and named SNI certificates and optional client certificate verification. Negotiation rejects plaintext auth and discards the SMTP greeting state at the STARTTLS boundary. SMTP rejects message submission until both authentication stages succeed. Credentials are restricted to bounded printable ASCII. See [RFC 3207](https://www.rfc-editor.org/rfc/rfc3207), [RFC 2595](https://www.rfc-editor.org/rfc/rfc2595) and [RFC 4616](https://www.rfc-editor.org/rfc/rfc4616).

The authentication service receives HTTP/1.0 GET with the NGINX `Auth-Method: plain`, `Auth-User`, `Auth-Pass`, `Auth-Protocol`, `Auth-Login-Attempt`, `Client-IP` and `Auth-SSL: on` headers. SMTP also sends the validated post-TLS HELO name. User and password use NGINX mail percent escaping. HTTP requires literal loopback. HTTPS verifies the service with the standard roots. Cauce ignores the response body, disallows redirects and retries, and requires a unique status header. Authorization requires `Auth-Status: OK` and a unique `Auth-Server` IP and `Auth-Port` that match the listener allowlist. Cauce forwards the original validated credentials to the selected backend. Credential replacement, `Auth-Wait`, certificate metadata headers, trusted PROXY input and unauthenticated SMTP delivery remain outside this subset. [NGINX auth_http contract](https://nginx.org/en/docs/mail/ngx_mail_auth_http_module.html).

Configure a backend `tls` object with `server_name` and `ca_file` for verified implicit TLS. It can also carry a client certificate. Cleartext backend links are allowed only on loopback. After origin authentication, an exclusive socket relays the remaining session with bounded buffers. The origin handles mailbox commands and authenticated SMTP message policy and delivery.

Per listener, `max_connections` defaults to 256, `auth_timeout_ms` to 15s, `idle_timeout_ms` to 5m and `session_timeout_ms` to 1h. The absolute authentication clock includes TLS, command parsing, the HTTP service and origin login. Negotiation caps command lines at 2 KiB, commands at 32 and policy denials at three. The metrics endpoint exposes only aggregate mail session and authentication counters. Mail listener settings and mail PEM changes require restart. Active relays may drain within the gateway shutdown deadline.

The [independent fixtures](../tests/mail/) exercise real Postfix 3.7.11 and Dovecot 2.3.19.1 using standard Python mail clients, with a pinned NGINX 1.30.8 STARTTLS differential.
