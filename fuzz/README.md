# HTTP/2 HEADERS parser fuzzing

This target invokes the production `HeaderObserver` through its `fuzzing`
feature entry point. It checks that one-buffer and byte-fragmented delivery at
an identical instant produce the same parse result and parser state. A separate
reference model checks generated valid HEADERS, CONTINUATION, DATA, truncated
payload, partial frame-header, and wrong-stream sequences. Deadline checks
advance a synthetic Tokio instant and assert that an open field block retains
its original deadline and expires instead of renewing as bytes trickle in.

The fuzzer is libFuzzer with Rust nightly's AddressSanitizer instrumentation,
and enables the isolated fuzz crate's `fuzzing` feature to expose production hooks.
The Rust base image is pinned to `1.88-bookworm` at digest
`sha256:af306cfa71d987911a781c37b59d7d67d934f49684058f96cf72079c3626bfe0`,
and the nightly toolchain is
pinned to `nightly-2025-10-01`; `cargo-fuzz` is pinned to `0.13.2`. The seed
corpus in `fuzz/corpus/header_observer/` is deterministic and replayable.

From the `outputs/cauce` directory run:

```sh
./fuzz/run-docker.sh
```

The runner uses Colima's Docker socket and starts Colima if `docker info`
fails. It runs a bounded campaign: seed 60616, at most 100,000 executions or
60 seconds, 4 KiB maximum input and 5 seconds per input. Campaign corpus,
crash artifacts and the full log are written to `work/fuzz6/`, which is
excluded from release archives. Reproduce any saved artifact with:

```sh
RUN_DIR=work/fuzz6/runs/<UTC-timestamp>
docker run --rm \
  -v "$PWD/$RUN_DIR/corpus:/corpus" \
  -v "$PWD/$RUN_DIR/artifacts:/artifacts" \
  cauce-h2-fuzz:nightly-2025-10-01 \
  /artifacts/CRASHFILE -runs=1
```

This fuzzer covers the framing/deadline observer, not Hyper's complete HTTP/2
state machine, HPACK decoder, TLS stack, or real network scheduling. Those
integration and resource-limit tests are in the Docker HTTP/2 stress suite.

## Recorded Docker campaign

The completed campaign used Colima's native `linux/arm64` image with image ID
`sha256:30cd009db9a466f9ac542fbc84efd30b2a215f812bb886f73fbfe3dded5856f8`,
Rust `1.92.0-nightly (fa3155a64 2025-09-30)`, and `cargo-fuzz 0.13.2`. ASan
was explicitly enabled. With seed 60616, libFuzzer completed 100,000 runs in
its reported 1 second, at 50,000 executions/second, found 156 new units, and
peaked at 84 MiB RSS. It loaded 41,823 instrumentation counters; the observer
function report shows 62/146 edges and the advancing-time helper 8/45 edges.
The seed corpus grew from 10 inputs to 86 saved inputs (4,260 bytes of file contents). No crash artifact
was produced. Full logs, toolchain version and image ID are retained in `docs/increment6-fuzz*`;
the generated corpus is under `work/fuzz6/` and excluded from release archives.

Each invocation starts from the checked-in seed files and writes a fresh
timestamped `work/fuzz6/runs/<UTC timestamp>/` directory, so reruns do not
silently inherit a prior campaign's expanded corpus.

Dependency notices for this separate test crate are in `third-party/manifest.json`.
The bundled C++ libFuzzer sources also carry the LLVM exception license; its
original text is preserved alongside the Rust wrapper notices.

## Content policy observer

The `content_policy` target shares the production range parser, static path
decoder and cache selector encoding. It checks bounds, disjoint ranges,
legal filename components and unambiguous bounded field encoding. The
same pinned toolchain and AddressSanitizer can run it with
`cargo +nightly-2025-10-01 fuzz run -s address content_policy -- -runs=100000`.
