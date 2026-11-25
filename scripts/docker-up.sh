#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if ! docker info >/dev/null 2>&1; then
    colima start
fi
docker compose -p cauce-local up --build -d
