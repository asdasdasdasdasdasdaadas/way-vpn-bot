#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env.production ]; then
    echo 'Create .env.production from .env.production.example and fill in the values.' >&2
    exit 1
fi
exec docker compose --env-file .env.production -f compose.production.yaml "$@"
