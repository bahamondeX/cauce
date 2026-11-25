FROM rust:1.88-bookworm@sha256:af306cfa71d987911a781c37b59d7d67d934f49684058f96cf72079c3626bfe0 AS build
RUN apt-get update && apt-get install -y --no-install-recommends cmake=3.25.1-1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN CARGO_NET_GIT_FETCH_WITH_CLI=true cargo build --locked --release

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
COPY --from=build /app/target/release/cauce-pingora-reference /usr/local/bin/
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/cauce-pingora-reference"]
