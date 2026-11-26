# Compile on the builder's native CPU; cross-link when target architecture differs.
FROM --platform=$BUILDPLATFORM rust:1.89-bookworm@sha256:948f9b08a66e7fe01b03a98ef1c7568292e07ec2e4fe90d88c07bb14563c84ff AS builder
ARG TARGETARCH
ARG BUILDARCH
RUN case "$TARGETARCH" in \
      amd64) cauce_target=x86_64-unknown-linux-gnu ;; \
      arm64) cauce_target=aarch64-unknown-linux-gnu ;; \
      *) exit 1 ;; esac \
    && if [ "$TARGETARCH" != "$BUILDARCH" ]; then \
      apt-get update && case "$TARGETARCH" in \
        amd64) apt-get install -y --no-install-recommends gcc-x86-64-linux-gnu libc6-dev-amd64-cross ;; \
        arm64) apt-get install -y --no-install-recommends gcc-aarch64-linux-gnu libc6-dev-arm64-cross ;; \
      esac && rm -rf /var/lib/apt/lists/*; fi \
    && rustup target add "$cauce_target"
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY schemas ./schemas
COPY examples/cauce.toml examples/cauce.yaml ./examples/
ARG CAUCE_PROFILE=full
RUN --mount=type=cache,id=cauce-registry,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,id=cauce-target-${TARGETARCH},target=/src/target,sharing=locked \
    case "$TARGETARCH" in \
      amd64) cauce_target=x86_64-unknown-linux-gnu; export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc ;; \
      arm64) cauce_target=aarch64-unknown-linux-gnu; export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc ;; \
    esac \
    && if [ "$TARGETARCH" = "$BUILDARCH" ]; then \
      unset CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER; fi \
    && cargo clean --release --target "$cauce_target" -p cauce \
    && cargo build --release --locked --no-default-features --features "$CAUCE_PROFILE" --target "$cauce_target" \
    && cp "/src/target/$cauce_target/release/cauce" /usr/local/bin/cauce-built \
    && cp "/src/target/$cauce_target/release/cauce-collector" /usr/local/bin/cauce-collector-built

FROM scratch AS release-artifacts
COPY --from=builder /usr/local/bin/cauce-built /cauce
COPY --from=builder /usr/local/bin/cauce-collector-built /cauce-collector

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN groupadd --gid 10001 cauce && useradd --uid 10001 --gid 10001 --no-create-home cauce \
    && mkdir -p /etc/cauce /var/log/cauce /var/lib/cauce && chown 10001:10001 /var/log/cauce /var/lib/cauce
COPY --from=builder /usr/local/bin/cauce-built /usr/local/bin/cauce
COPY --from=builder /usr/local/bin/cauce-collector-built /usr/local/bin/cauce-collector
COPY examples/docker.toml /etc/cauce/cauce.toml
COPY LICENSE /usr/share/doc/cauce/LICENSE
COPY third-party /usr/share/doc/cauce/third-party
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/local/bin/cauce"]
CMD ["/etc/cauce/cauce.toml"]
