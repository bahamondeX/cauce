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

## AWS estimate and deployment commands

The AWS connector and CLI required reauthentication during Milestone 2B.
The identity, instance inventory and effective permissions remain unverified.
The public N. Virginia price list published on 2026-11-24 supplies the following
Linux Shared On-Demand rates. [AWS bulk price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv)
[Price rows and calculation](../bench/program/evidence/milestone-2b/aws-price-extract.json).

| Architecture | Example worker host | CPU budget | Linux hourly |
| --- | --- | --- | ---: |
| ARM64 | `c7g.4xlarge` | 16 vCPUs | $0.580 |
| AMD64 | `c7i.8xlarge` | 16 physical cores when configured with one thread per core | $1.428 |

Four hours on each architecture and two 40 GB gp3 root volumes cost about
**$8.07** at list rates, using a 720-hour month for the storage estimate. Add
network costs, taxes and any required SSM connectivity. Check PMU and
CPU placement after boot. The selected instance size gives separate
roles room for the eight-worker scaling cells.
[EC2 instance specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html)
[Supported CPU options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/cpu-options-supported-instances-values.html).

Use an existing private subnet, security group and instance profile with
Systems Manager access. The operator supplies their approved AWS profile,
account and region, the architecture-specific Linux AMI, its root device name,
an existing EC2 key pair for SSH over Session Manager, and a unique run ID.
Verify subnet reachability to SSM and package or artifact sources. Inspect
availability, caller identity and the permission dry-run before authorizing
a launch. The launch is billable.

```bash
aws sts get-caller-identity --profile "$profile" --region "$region"
aws ec2 describe-instance-type-offerings --profile "$profile" --region "$region" \
  --filters "Name=instance-type,Values=$instance_type"
aws ec2 describe-images --profile "$profile" --region "$region" --image-ids "$ami"

# Use cpu_options=() for c7g.4xlarge.
cpu_options=(--cpu-options CoreCount=16,ThreadsPerCore=1)  # c7i.8xlarge
launch=(aws ec2 run-instances --profile "$profile" --region "$region" \
  --image-id "$ami" --instance-type "$instance_type" --count 1 --client-token "$run_id" \
  --subnet-id "$subnet" --security-group-ids "$security_group" \
  --no-associate-public-ip-address --key-name "$key_pair" \
  --iam-instance-profile "Name=$instance_profile" --metadata-options HttpTokens=required \
  --block-device-mappings "DeviceName=$root_device,Ebs={VolumeType=gp3,VolumeSize=40,Encrypted=true,DeleteOnTermination=true}" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=CauceBenchmarkRun,Value=$run_id}]" \
  "${cpu_options[@]}")
"${launch[@]}" --dry-run
```

On an explicitly authorized temporary run, the operator removes `--dry-run`
and captures the instance ID from `--query 'Instances[0].InstanceId'
--output text`. In the same shell, install an exit trap scoped to that instance
ID and run tag. Recheck the account and tag immediately before termination.

```bash
current_account=$(aws sts get-caller-identity --profile "$profile" --region "$region" \
  --query Account --output text) || exit
[[ "$current_account" == "$authorized_account" ]] || exit 2
instance=$("${launch[@]}" --query 'Instances[0].InstanceId' --output text) || exit
cleanup() {
  current=$(aws sts get-caller-identity --profile "$profile" --region "$region" \
    --query Account --output text) || return
  [[ "$current" == "$authorized_account" && "$instance" == i-* ]] || return
  tag=$(aws ec2 describe-tags --profile "$profile" --region "$region" \
    --filters "Name=resource-id,Values=$instance" "Name=key,Values=CauceBenchmarkRun" \
    --query 'Tags[0].Value' --output text) || return
  [[ "$tag" == "$run_id" ]] || return
  aws ec2 terminate-instances --profile "$profile" --region "$region" --instance-ids "$instance"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
aws ec2 wait instance-running --profile "$profile" --region "$region" --instance-ids "$instance"
aws ssm start-session --profile "$profile" --region "$region" --target "$instance"
```

Transfer a Git bundle and use SSH over Session Manager with the approved key.
Run the commands above under the installed toolchain, then retrieve the
result directory and verify `SHA256SUMS` before leaving the shell.
[SSH through Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-enable-ssh-connections.html).
