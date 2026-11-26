# Build profiles

Full remains the default. Run `cauce capabilities --json` to inspect the
features compiled into a binary. `cauce explain cauce.yaml` validates its
configuration and reports listener counts and enabled capabilities without
printing credentials, TLS paths or configured endpoints.

| Cargo feature | Available protocols and optional features |
| --- | --- |
| `core` | HTTP/1.1, HTTP/2, gRPC, TLS, routing, pooling, DNS and metrics |
| `web` | Core plus static files, compression and HTTP memory and disk cache |
| `network` | Core plus TCP, UDP, Stream TLS and TLS passthrough |
| `edge` | Web plus HTTP/3, QUIC and ACME |
| `application` | Core plus FastCGI, uWSGI and SCGI |
| `full` | Edge, Network, Application and mail |

Build a specific profile with `cargo build --release --locked
--no-default-features --features core --bin cauce`. Slim builds reject
configuration that requires an absent capability. QUIC, ACME and
compression dependencies are Cargo-gated. Shared configuration types,
security checks and code used by multiple profiles remain in common
source. The collector continues to build as a separate executable.

`scripts/stage-native-release.sh OUTPUT REF` builds Full and Core for
Linux ARM64 and AMD64. Pass `full arm64` after the revision to stage only
that pair. Each pair has its own source identity and `SHA256SUMS` after
the build and ELF architecture checks succeed.

[Profile smoke checks](../scripts/check-build-profiles.sh)
[Checksummed Linux staging](../scripts/stage-native-release.sh).
