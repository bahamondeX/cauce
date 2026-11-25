FROM rust:1.88-bookworm@sha256:af306cfa71d987911a781c37b59d7d67d934f49684058f96cf72079c3626bfe0
RUN apt-get update && apt-get install -y --no-install-recommends clang llvm libclang-dev ca-certificates && rm -rf /var/lib/apt/lists/* \
 && rustup toolchain install nightly-2025-10-01 --profile minimal \
 && cargo +nightly-2025-10-01 install cargo-fuzz --version 0.13.2 --locked
WORKDIR /src
COPY . /src
ENV CARGO_HOME=/cargo CARGO_TARGET_DIR=/target
ENTRYPOINT ["cargo", "+nightly-2025-10-01", "fuzz", "run", "header_observer", "--features", "fuzzing", "--sanitizer", "address", "--", "-print_final_stats=1"]
