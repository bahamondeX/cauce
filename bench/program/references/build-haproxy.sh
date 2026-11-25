#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo 'usage: build-haproxy.sh HAPROXY-3.4.8.tar.gz OUTPUT_DIRECTORY' >&2
    exit 2
fi

source_dir=$(cd "$(dirname "$1")" && pwd)
archive="$source_dir/$(basename "$1")"
mkdir "$2"
output=$(cd "$2" && pwd)
builder=rust:1.88-bookworm@sha256:af306cfa71d987911a781c37b59d7d67d934f49684058f96cf72079c3626bfe0
docker run --rm --network none \
    -v "$archive:/inputs/haproxy.tar.gz:ro" -v "$output:/results" "$builder" \
    sh -ec '
    printf "%s  %s\n" 024bd521a6cbbe32b426853147e6bbd6a89b107c5a34144846193fee5b7c6ed1 /inputs/haproxy.tar.gz | sha256sum -c
    gcc --version | head -1
    mkdir /build
    tar --strip-components=1 -xzf /inputs/haproxy.tar.gz -C /build
    make -C /build -j2 TARGET=linux-glibc
    install -m 0755 /build/haproxy /results/haproxy
    /results/haproxy -vv
    sha256sum /results/haproxy
    '
