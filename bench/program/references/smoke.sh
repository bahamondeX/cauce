#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    echo 'usage: smoke.sh LOAD_BINARY HAPROXY_BINARY OUTPUT_DIRECTORY' >&2
    exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
load=$(realpath "$1")
haproxy=$(realpath "$2")
mkdir "$3"
out=$(realpath "$3")
project="cauce-ref-$$"
backend=cauce-increment7-backend:local
client=python:3.13.2-slim-bookworm@sha256:6b3223eb4d93718828223966ad316909c39813dee3ee9395204940500792b740
pingora=cauce-pingora-reference:0.9.1
debian=debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc

docker network create --internal "$project" >/dev/null
cleanup() {
    docker rm -f "$project-pingora" "$project-haproxy" "$project-backend" >/dev/null 2>&1 || true
    docker network rm "$project" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker image inspect --format '{{.Id}} {{.Architecture}} {{json .RepoTags}}' \
    "$backend" "$client" "$pingora" "$debian" >"$out/images.txt"
docker run -d --pull never --name "$project-backend" --network "$project" --network-alias backend \
    --cpuset-cpus 1 --cpus 1 --memory 256m -e TLS=0 "$backend" >/dev/null
upstream=$(docker inspect --format "{{(index .NetworkSettings.Networks \"$project\").IPAddress}}" "$project-backend")
docker run -d --pull never --name "$project-pingora" --network "$project" --network-alias pingora \
    --cpuset-cpus 0 --cpus 1 --memory 256m "$pingora" 0.0.0.0:8080 "$upstream:8000" >/dev/null
docker run -d --pull never --name "$project-haproxy" --network "$project" --network-alias haproxy \
    --cpuset-cpus 0 --cpus 1 --memory 256m -u 10001:10001 \
    -v "$haproxy:/usr/local/bin/haproxy:ro" -v "$here/haproxy-clear.cfg:/etc/haproxy.cfg:ro" \
    --entrypoint /usr/local/bin/haproxy "$debian" -db -f /etc/haproxy.cfg >/dev/null

for name in pingora haproxy; do
    docker run --rm --pull never --network "$project" "$client" python -c "
import time, urllib.request
for _ in range(30):
    try:
        assert urllib.request.urlopen('http://$name:8080/echo', timeout=1).read() == b'x' * 1024
        break
    except OSError:
        time.sleep(.2)
else:
    raise RuntimeError('fixture did not become ready')
"
    printf '{"url":"http://%s:8080/echo","protocol":"http1","rate":50,"seconds":2,"warmup_seconds":1,"concurrency":32,"queue":64,"request_bytes":0,"response_bytes":1024,"response_byte":120}\n' "$name" >"$out/$name.spec.json"
    docker run --rm --pull never --network "$project" --cpuset-cpus 2 --cpus 1 --memory 256m \
        -v "$load:/cauce-load:ro" -v "$out:/results" --entrypoint /cauce-load "$client" \
        "/results/$name.spec.json" "/results/$name.jsonl" "/results/$name.summary.json"
    PYTHONPATH="$here/.." python3 - "$out" "$name" <<'PY'
import json
from pathlib import Path
import sys
from capacity import audit, write_json
root, name = Path(sys.argv[1]), sys.argv[2]
spec, summary = [json.loads((root / f"{name}.{suffix}.json").read_text()) for suffix in ("spec", "summary")]
result = audit(root / f"{name}.jsonl", summary, spec)
write_json(root / f"{name}.audit.json", result)
assert not result["invalid_reasons"] and result["successful"] == result["offered"]
assert result["warmup_successes"] == result["warmup_offers"]
print(f"{name}: {result['successful']} successful measured responses")
PY
    docker logs "$project-$name" >"$out/$name.log" 2>&1
done
