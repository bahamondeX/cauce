# Benchmark methodology

Record source revision, Cargo.lock digest, compiler, image ID, binary SHA-256,
libc, TLS provider and allocator with every release comparison. Keep environment
manifests separate by architecture. Record bare metal or virtualization, kernel,
CPU topology, affinities, cgroup quotas, clock source and pressure counters.
Run client, backend and sampler outside the proxy's CPU and memory budgets.
Use the same fixtures, budgets and verified transport policy across stacks.

## Generator qualification

The Rust generator is a separate Cargo package under `bench/program/load`.
Its Linux or Darwin scheduler thread derives each deadline from a monotonic
origin and the integer offer index. It sleeps until the configured spin window,
then checks the absolute deadline. A bounded channel carries offers to persistent
HTTP/1.1 or HTTP/2 client connections. Its dispatcher limits in-flight work.
Every offer records scheduled, arrival, dispatch and completion offsets as
integer nanoseconds. Arrivals that miss the cutoff and full-queue rejections get
their own rows. Requests can also expire in the queue. Connection retries and
redirects are disabled. HTTPS verifies the hostname and chain with the explicit
fixture CA. Successful responses must match the expected version, size and byte
pattern. HDR buckets and exact rows are emitted after measurement.

Run a direct generator-to-backend control before and after target measurements
at twice the highest target offer rate. Keep CPU affinity, protocol and payload
consistent. The qualification gate rejects scheduling p99 above 10 ms, missing
or duplicate offers, scheduler drops and a control with transport or integrity
errors. The SLO runner also requires the control to meet the chosen latency and
goodput SLOs. Record jitter and host pressure inside the run. Reject a comparison
when the generator or backend becomes the bottleneck.

## Capacity and paired comparisons

Declare the workload's p99, p99.9, minimum offer-window goodput fraction and
maximum error fraction before running. The capacity runner executes strictly
increasing rates, stops at the first failure and confirms the highest qualifying
rate for a longer interval. Confirm only the rates actually tested. End-to-end
latency starts at the scheduled arrival. Positive goodput excludes rejected,
invalid, and late-completing requests. Report scheduler, queue and service
latency independently. Keep warm-up traffic and rows available for accounting.

For optimization comparisons, randomize cell order within each seeded repetition.
Pair repetitions by workload, rate, worker allocation and log mode. Report
paired median differences and bootstrap intervals after at least seven valid
pairs. Preserve all rejected cells with reasons. A fixed-rate characterization
is separate from a sustainable capacity campaign. Cache, transport, resource,
and TLS settings belong in every scenario definition.

## Reproducing the tools

```sh
cargo build --release --locked --manifest-path bench/program/load/Cargo.toml
bench/program/load/target/release/cauce-load spec.json offers.jsonl summary.json

taskset -c 2 python3 bench/program/capacity.py /path/to/cauce-load \
  target.json direct-backend.json /tmp/cauce-capacity \
  --rates 100 250 500 --confirm-seconds 60 --p99-ms 10 --p999-ms 40

python3 bench/program/capacity-workload.py /path/to/cauce /path/to/cauce-load \
  /tmp/cauce-echo --protocol http1 --workers 1 --proxy-cpus 0 \
  --rates 100 250 400 --confirm-seconds 20 --p99-ms 20 --p999-ms 100

python3 bench/program/profile-linux.py "$proxy_pid" /tmp/cauce-profile \
  --mode record --seconds 12 --flamegraph /path/to/FlameGraph
```

The generator spec has `url`, `protocol` (`http1` or `http2`), integer `rate`,
`seconds`, `warmup_seconds`, `concurrency`, `queue`, `request_bytes`,
`response_bytes` and `response_byte`. It accepts optional `host`, `ca_file`,
`timeout_ms`, `drop_lag_ms`, and `spin_us`. Nonempty request payloads select POST.
All payload bytes sent are ASCII `x`. Optional marker files coordinate resource
counter snapshots with a benchmark controller. Use an unused output directory.

Profiling is a diagnostic workload. Keep its overhead out of the primary timing
campaign. Use a matching source build with debug information and capture user-space
stacks. Measure hardware counters and tracepoints only when the kernel exposes
them and explicitly permits access. Record unavailable events separately from
zero counter values.

The local echo capacity fixture needs Node and separate Linux CPU affinities.
Use the direct spec form with externally provisioned, identical cgroups for a
release memory or CPU comparison. The standalone profiler also accepts a live
proxy PID from any of the existing TLS, gRPC, SSE or recovery fixtures.

Generate the Increment 8 fixture certificates and profile validated gRPC
messages over cleartext HTTP/2. Repeat with `--method Bidi` and a distinct
output directory to capture streaming. Use a symbolized proxy build.

```sh
sh bench/increment8/make-certs.sh /tmp/cauce-profile-certs
docker build -t cauce-increment8-grpc:local -f bench/increment8/backend/Dockerfile.grpc bench/increment8/backend
python3 bench/program/profile-grpc-workload.py /path/to/cauce /tmp/cauce-profile-certs \
  /tmp/cauce-grpc-unary --method Unary --rate 140 --seconds 12 \
  --backend-image cauce-increment8-grpc:local --flamegraph /path/to/FlameGraph --sudo
```

Run the three-way HTTP/1.1 Docker replay from the repository root. Build both
Cauce images from their recorded source trees with the same Dockerfile cache fix.
Use fresh extract directories and the daemon's native architecture. On ARM64
the commands below use the same image tags as the recorded run.

```sh
contexts=$(mktemp -d)
mkdir "$contexts/published" "$contexts/checkpoint"
git archive ba6f95277f5ffca966faea5e25203c1b75d310d5 | tar -x -C "$contexts/published"
git archive f754e44eb878c89281797c33203f0dd49885d75c | tar -x -C "$contexts/checkpoint"
for variant in published checkpoint; do
  git show 845e0a5 --format= -- Dockerfile | patch -d "$contexts/$variant" -p1
  docker buildx build --builder default --platform linux/arm64 --load \
    -t "cauce:milestone2-$variant-clean" "$contexts/$variant"
done
```

```sh
python3 bench/increment7/run.py \
  --image cauce:milestone2-published-clean --candidate-image cauce:milestone2-checkpoint-clean \
  --nginx-image nginx:1.30.8-alpine@sha256:8eb04fe3b385090e9aa02961f77398ad09b6bf33ab07f6669a5bb7acce22c0d3 \
  --native-load bench/program/load/target/release/cauce-load --scenarios normal \
  --rate 250 --seconds 8 --warmup 3 --repetitions 3 --results-root /tmp/cauce-three-way
```

The container client requires a Linux binary for the Docker daemon's
architecture and a glibc compatible with the Debian Bookworm fixture.
The Docker runner enforces the 32-active-request admission and post-overload
recovery contract before every cell. Its separate sampler, client and backend
default to CPUs 3, 2 and 1, respectively. The raw native offer file stays
byte-for-byte intact. The historical summary receives a projection of those rows.

For either native Linux architecture, capture the effective runtime environment
from the actual runner before each campaign. Include the proxy and generator
binaries to bind the manifest to those builds.

```sh
sh bench/program/linux-environment.sh /path/to/cauce /path/to/cauce-load > linux-environment.txt
```
