# Native Linux benchmark lab

Run on Linux AArch64 or x86-64 with the native instruction set. Record whether
the host is bare metal or virtualized and whether the PMU is exposed. The
Colima guest is useful for Linux ARM64 correctness and diagnostic captures.
Its four vCPUs, scheduler variability and absent hardware PMU leave the
native performance gates open.

## Local or already authorized Linux host

Install Rust 1.88.0 through rustup, Node 24, Python 3.11+, Git, tar, util-linux
and the perf package matching the running kernel. Keep the OS, Node version
and compiler identical within each A/B comparison. Check the physical-core
and SMT map before choosing the CPU indices.

```sh
lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE,MAXMHZ,MINMHZ
cat /sys/fs/cgroup/cpu.max
perf list
rustup toolchain install 1.88.0 --profile minimal
python3 bench/program/native-linux.py /var/tmp/cauce-arm64-http1 \
  --revision HEAD --proxy-cpus 0 1 2 3 4 5 6 7 --generator-cpus 8 9 \
  --backend-cpu 10 --sampler-cpu 11 --workers 1 2 4 8 \
  --confirm-seconds 60 --stability-seconds 180
(cd /var/tmp/cauce-arm64-http1 && sha256sum -c SHA256SUMS)
```

Choose separate physical cores for proxy, generator, backend and sampler.
The first one, two, four or eight listed proxy CPUs go to the corresponding
worker configuration. The runner rejects shared SMT siblings across roles.
Check that effective CPU quotas allow all selected roles to run concurrently.
Use a quiet host and record governor and turbo policy. Frequency snapshots
appear as empty when the kernel exposes no per-CPU frequency files.

The runner archives each requested Cauce revision into a temporary directory
and compiles it with the locked dependencies. It runs TCP loopback against the
pinned repository fixture. The tested rate controls use identical payloads and
client settings. A direct control at twice each new maximum target rate runs
before the associated target cell. A final control rechecks the highest
qualified control rate. The controller keeps raw warm-up and measurement
offers, target/control audits, the sampler's process CPU ticks and RSS,
configuration, release identities, capacity and scaling CSVs, and checksums.
An SLO failure terminates a ramp or bounds refinement. A failed direct
control establishes a generator/backend ceiling for that campaign. Use
`--revision` more than once to exercise multiple revisions with the same
generator. Use the paired runner for optimization claims.

The 180-second example fits the generator's two-million-offer limit at up to
11,111 requests/s. Choose the stability rate and duration within that limit
or increase generator capacity and requalify the direct controls before a
longer high-rate hold. The 60-second confirmation fits the default ramp
through 32,000 requests/s.

For a Docker comparison, use the established per-proxy cgroup CPU and memory
budgets, backend admission gate and independently audited normal workload.
Capture CPU profiles in a separate campaign with matching symbolized builds.
Use `--frequency` for longer software sampling, `--events` to select a distinct
tracepoint pass and `--heaptrack` for whole-process allocation traces.
A heaptrack capture includes startup, warm-up and shutdown.

## AWS preflight and estimate

Milestone 2C restored the existing AWS CLI session and completed a read-only
inventory. There were no reusable EC2 hosts or managed instances across the
17 enabled regions. N. Virginia has a 16-vCPU Standard On-Demand quota.
`c7i.8xlarge` consumes 32 quota vCPUs even with SMT disabled. The approved
account's default VPC has an Internet Gateway and public subnets in five AZs
that offer both proposed types. PMU access is unverified for each exact guest.
[AWS CPU options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-optimize-cpu.html)
[Compute instance specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html).

| Architecture | Host | Dedicated roles | Linux Shared On-Demand |
| --- | --- | --- | ---: |
| ARM64 | `c7g.4xlarge` | 16 Graviton3 cores | $0.580/hour |
| AMD64 | `c7a.4xlarge` | 16 AMD EPYC cores | $0.82112/hour |
| AMD64, previous plan | `c7i.8xlarge` | 16 Intel cores with SMT disabled | $1.428/hour |

Four hours each on the original ARM64 and Intel pair would cost **$8.07**
for compute and two 40 GiB gp3 roots at $0.08/GiB-month, using a 720-hour
month. With one ephemeral public IPv4 per host, that estimate is **$8.11**.
Running the Graviton and AMD pair sequentially totals **$5.68** including
the same storage and $0.04 for eight public IPv4 hours. TCP loopback benchmark
traffic stays on each VM. The EC2 estimate excludes evidence egress,
any charge for exceeding the account's shared transfer allowance, and
account-specific taxes. One extra hour on the ARM64 host is about $0.59
and one on AMD64 about $0.83, including the root and IPv4. A persistent NAT
gateway would add $0.045/hour plus $0.045/GB; the proposed launch uses the
existing Internet Gateway and one temporary source-restricted SSH rule.
[AWS EC2 catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv)
[AWS VPC pricing](https://aws.amazon.com/vpc/pricing/)
[AWS public pricing](https://aws.amazon.com/ec2/pricing/on-demand/).

## Launch and evidence retrieval

[Host manifests](../bench/program/native-linux-2c.toml) record the proposed
environments. Refresh the existing browser session only when STS fails.
Choose an existing listed subnet with an Internet Gateway route for this
temporary test. Set `ssh_cidr` to the operator's public IP followed by
`/32`, and provide the matching SSH key pair's private key path.

```bash
aws login --profile default
aws sts get-caller-identity --profile default --region us-east-1
aws ec2 describe-subnets --profile default --region us-east-1 \
  --query 'Subnets[?MapPublicIpOnLaunch].{zone:AvailabilityZone,subnet:SubnetId}'
aws ec2 describe-instance-type-offerings --profile default --region us-east-1 \
  --location-type availability-zone --filters Name=instance-type,Values=c7g.4xlarge,c7a.4xlarge
```

After explicit authorization, run both commands serially from the committed
candidate checkout. The second host fits the quota after the first is
terminated. The runner uses the current Canonical 24.04 AMI parameter,
validates its publisher, and records the resolved AMI in the evidence.

```bash
bench/program/aws-native-lab.sh arm64 default "$authorized_account" "$subnet" \
  "$ssh_cidr" "$ssh_key" "$evidence_root" &&
bench/program/aws-native-lab.sh amd64 default "$authorized_account" "$subnet" \
  "$ssh_cidr" "$ssh_key" "$evidence_root"
```

The runner uses a scoped temporary security group and tagged instance and
volume. It bundles the candidate revision, transfers it over SSH, verifies
the pinned Node archive checksum, runs locked release builds, and retrieves
results with checksums. SSH uses a per-run known-hosts file. An exit trap
verifies the account and instance tag, waits for termination, checks for
remaining tagged volumes and removes the temporary security group. An
instance-side shutdown timer requests termination about 225 minutes after
cloud-init completes if the controller loses contact. If cloud-init fails
or the controller exits without running its trap, use the saved instance ID
and tag, verify the account and tag, terminate that instance, and delete the
tagged temporary group after termination.

The PMU probe records each hardware event separately with `perf stat` and
a CPU workload. Treat missing files, errors, `<not supported>` and
`<not counted>` as unavailable. Zero is meaningful only for an event that
actually counted. Inspect topology and system state before qualifying cells.
EC2 offers multiple Nitro form factors and guest PMU capability must be
observed in the running guest.
[AWS Nitro instance form factors](https://docs.aws.amazon.com/ec2/latest/instancetypes/ec2-nitro-instances.html).

Provisioning and cleanup permissions have not been exercised against live
resources. This preflight created no resources and left no lab charges.
