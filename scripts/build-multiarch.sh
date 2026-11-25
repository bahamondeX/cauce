#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if ! docker info >/dev/null 2>&1; then colima start; fi
builder=cauce-multiarch
if ! docker buildx inspect "$builder" >/dev/null 2>&1; then
    docker buildx create --name "$builder" --driver docker-container >/dev/null
fi
# Named builder does not change the user's selected builder. No registry publication.
docker buildx build --builder "$builder" --platform linux/amd64,linux/arm64 \
    --output type=oci,dest=cauce-multiarch.tar .
