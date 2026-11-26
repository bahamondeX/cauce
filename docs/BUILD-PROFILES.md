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

On Darwin, `scripts/stage-zig-linux.sh` can cross-link a single
architecture and profile with repository-local build state. Run it once
per profile and architecture, using `full/linux_arm64`, `core/linux_arm64`,
`full/linux_amd64` and `core/linux_amd64` below one staging directory.
`scripts/stage-oci-layout.py ELF_MATRIX OUTPUT` packages those exact
executables into separate Full and Core multi-architecture OCI layouts
over the Dockerfile's pinned Debian base. The layouts run with numeric
UID/GID 10001 and do not use a container daemon for assembly.

Core was 26.0% smaller than Full on Darwin ARM64. The separately matched
Linux checkpoint reduced the executable by 26.8% on ARM64 and 26.0% on
AMD64. The feature checks, process samples, Mach-O sections and binary
identities are in the milestone evidence.

[Profile smoke checks](../scripts/check-build-profiles.sh)
[Checksummed Linux staging](../scripts/stage-native-release.sh)
[Darwin cross-link staging](../scripts/stage-zig-linux.sh)
[OCI layout staging](../scripts/stage-oci-layout.py)
[Measured profile comparison](PERFORMANCE-MILESTONE-9.md#distribution-checks).
