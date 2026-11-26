#!/usr/bin/env bash
# Run one explicitly authorized, disposable EC2 capacity cell.
set -Eeuo pipefail
if [[ $# != 7 ]]; then
    echo "usage: $0 {arm64|amd64} PROFILE EXPECTED_ACCOUNT SUBNET SSH_CIDR SSH_PRIVATE_KEY OUTPUT" >&2
    exit 2
fi
arch=$1 profile=$2 expected=$3 subnet=$4 cidr=$5 key=$6 out=$7
case "$arch" in
    arm64) instance_type=c7g.4xlarge; node_arch=arm64 ;;
    amd64) instance_type=c7a.4xlarge; node_arch=x64 ;;
    *) exit 2 ;;
esac
[[ $cidr =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/32$ && -f $key && -f $key.pub ]]
key_type=$(awk 'NR==1 { print $1 }' "$key.pub")
key_blob=$(awk 'NR==1 { print $2 }' "$key.pub")
[[ $key_type =~ ^ssh-(ed25519|rsa)$ && $key_blob =~ ^[A-Za-z0-9+/]+=*$ ]]
aws_lab() { aws --no-cli-pager --profile "$profile" --region us-east-1 "$@"; }
[[ $(aws_lab sts get-caller-identity --query Account --output text) == "$expected" ]]
mkdir -p "$out"
out=$(cd "$out" && pwd)
tag="cauce-2c-${arch}-$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
mkdir "$out/$tag"
out="$out/$tag"
token=$tag
group=
instance=
launch_started=false
cleanup() {
    local result=$?
    trap - EXIT
    set +e
    if [[ $(aws_lab sts get-caller-identity --query Account --output text) != "$expected" ]]; then
        echo "Could not revalidate the cleanup account for $tag" >&2
        exit 1
    fi
    if $launch_started && [[ -z $instance ]]; then
        instance=$(aws_lab ec2 describe-instances --filters "Name=client-token,Values=$token" \
            "Name=tag:CauceLab,Values=$tag" --query 'Reservations[].Instances[].InstanceId | [0]' --output text)
    fi
    if [[ $instance == i-* ]]; then
        if [[ $(aws_lab ec2 describe-instances --instance-ids "$instance" \
            --query "Reservations[0].Instances[0].Tags[?Key=='CauceLab'].Value | [0]" --output text) != "$tag" ]]; then
            echo "Could not verify ownership of $instance" >&2
            exit 1
        fi
        aws_lab ec2 terminate-instances --instance-ids "$instance" &&
            aws_lab ec2 wait instance-terminated --instance-ids "$instance" || result=1
        aws_lab ec2 describe-volumes --filters "Name=tag:CauceLab,Values=$tag" \
            --query 'Volumes[].{id:VolumeId,state:State}' --output json >"$out/post-cleanup-volumes.json" || result=1
        if [[ $(tr -d '[:space:]' <"$out/post-cleanup-volumes.json") != '[]' ]]; then
            echo "Tagged volumes remain or their deletion could not be verified" >&2
            result=1
        fi
    fi
    if [[ -n $group ]]; then
        aws_lab ec2 delete-security-group --group-id "$group" || result=1
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
git bundle create "$out/source.bundle" HEAD
vpc=$(aws_lab ec2 describe-subnets --subnet-ids "$subnet" --query 'Subnets[0].VpcId' --output text)
ami=$(aws_lab ssm get-parameter \
    --name "/aws/service/canonical/ubuntu/server/24.04/stable/current/$arch/hvm/ebs-gp3/ami-id" \
    --query Parameter.Value --output text)
root=$(aws_lab ec2 describe-images --owners 099720109477 --image-ids "$ami" \
    --query 'Images[0].RootDeviceName' --output text)
[[ $vpc == vpc-* && $ami == ami-* && $root == /dev/* ]]
cat >"$out/cloud-init.yaml" <<CLOUD
#cloud-config
ssh_authorized_keys:
  - $key_type $key_blob
runcmd:
  - [shutdown, -h, '+225', 'Cauce lab TTL']
CLOUD
group=$(aws_lab ec2 create-security-group --vpc-id "$vpc" --group-name "$tag" \
    --description "Temporary Cauce lab SSH" --query GroupId --output text)
aws_lab ec2 create-tags --resources "$group" --tags "Key=CauceLab,Value=$tag"
aws_lab ec2 authorize-security-group-ingress --group-id "$group" --protocol tcp --port 22 --cidr "$cidr"
launch_started=true
instance=$(aws_lab ec2 run-instances --client-token "$token" --image-id "$ami" --instance-type "$instance_type" \
    --count 1 --subnet-id "$subnet" --security-group-ids "$group" --associate-public-ip-address \
    --instance-initiated-shutdown-behavior terminate --metadata-options HttpTokens=required \
    --block-device-mappings "[{\"DeviceName\":\"$root\",\"Ebs\":{\"VolumeSize\":40,\"VolumeType\":\"gp3\",\"Encrypted\":true,\"DeleteOnTermination\":true}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=CauceLab,Value=$tag}]" "ResourceType=volume,Tags=[{Key=CauceLab,Value=$tag}]" \
    --user-data "file://$out/cloud-init.yaml" --query 'Instances[0].InstanceId' --output text)
printf '%s\n' "$instance" >"$out/instance-id"
aws_lab ec2 wait instance-running --instance-ids "$instance"
ip=$(aws_lab ec2 describe-instances --instance-ids "$instance" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
connection=(-i "$key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=$out/known-hosts")
host="ubuntu@$ip"
for ((attempt=0; attempt<60; attempt++)); do
    if ssh "${connection[@]}" "$host" true 2>/dev/null; then break; fi
    sleep 2
done
scp "${connection[@]}" "$out/source.bundle" "$host:source.bundle"
if ssh "${connection[@]}" "$host" bash -s -- "$node_arch" "$instance_type" "$ami" <<'REMOTE' >"$out/remote.log" 2>&1
set -Eeuo pipefail
node_arch=$1 instance_type=$2 ami=$3
mkdir -p "$HOME/results"
cloud-init status --wait
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo apt-get install -y ca-certificates curl git build-essential pkg-config python3 util-linux linux-tools-common
sudo apt-get install -y "linux-tools-$(uname -r)" || true
curl --proto '=https' --tlsv1.2 -fsSLo rustup.sh https://sh.rustup.rs
sh rustup.sh -y --profile minimal --default-toolchain 1.89.0
export PATH="$HOME/.cargo/bin:$PATH"
node_version=24.11.1
node_file="node-v$node_version-linux-$node_arch.tar.xz"
node_base="https://nodejs.org/dist/v$node_version"
curl -fsSLO "$node_base/SHASUMS256.txt"
curl -fsSLO "$node_base/$node_file"
awk -v file="$node_file" '$2 == file' SHASUMS256.txt | sha256sum -c -
tar -xf "$node_file"
export PATH="$HOME/node-v$node_version-linux-$node_arch/bin:$PATH"
git clone source.bundle cauce
cd cauce
{
    printf 'instance_type=%s\nami=%s\n' "$instance_type" "$ami"
    uname -a
    lscpu -e
    ls /sys/bus/event_source/devices
    node --version
    rustc -Vv
    git rev-parse HEAD
    cat /sys/fs/cgroup/cpu.max
} >"$HOME/results/host.txt"
for event in cycles instructions branches branch-misses L1-dcache-load-misses LLC-load-misses; do
    if sudo perf stat -x, -o "$HOME/results/pmu-$event.csv" -e "$event" -- \
        python3 -c 'sum(range(15_000_000))' 2>"$HOME/results/pmu-$event.stderr"; then
        true
    else
        printf 'perf event command failed\n' >>"$HOME/results/pmu-$event.stderr"
    fi
done
python3 bench/program/native-linux.py "$HOME/results/http1" --revision HEAD \
    --proxy-cpus 0 1 2 3 4 5 6 7 --generator-cpus 8 9 --backend-cpu 10 --sampler-cpu 11 \
    --workers 1 2 4 8 --confirm-seconds 60 --stability-seconds 180
REMOTE
then
    benchmark_status=0
else
    benchmark_status=$?
fi
ssh "${connection[@]}" "$host" tar -C . -czf - results >"$out/results.tar.gz"
(
    cd "$out"
    shasum -a 256 results.tar.gz remote.log >SHA256SUMS
)
exit "$benchmark_status"
