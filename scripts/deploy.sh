#!/bin/sh
# Run from a checked-out copy on the target Linux server.
set -eu
umask 077
cd "$(dirname "$0")/.."
mode=${1:-deploy}
case "$mode" in
    deploy|--update) ;;
    --help|-h)
        echo 'Usage: ./scripts/deploy.sh [--update]'
        echo 'First run configures the server; --update backs up and fast-forwards main before deployment.'
        exit 0 ;;
    *) echo 'Unknown option. Use --help.' >&2; exit 1 ;;
esac
if [ "$(uname -s)" != Linux ]; then
    echo 'Run this script on the target Linux server.' >&2
    exit 1
fi
for dependency in docker python3 flock; do
    command -v "$dependency" >/dev/null 2>&1 || { echo "Missing dependency: $dependency" >&2; exit 1; }
done
docker compose version >/dev/null
docker info >/dev/null 2>&1 || { echo 'Docker is unavailable or permission is missing.' >&2; exit 1; }
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is already running.' >&2; exit 1; }
python3 scripts/configure.py
./scripts/production.sh config --quiet

# Build before touching the running service. Existing installs require a backup.
if [ "$mode" = '--update' ]; then
    command -v git >/dev/null
    [ "$(git branch --show-current)" = main ] || { echo 'Switch to main before updating.' >&2; exit 1; }
    [ -z "$(git status --porcelain)" ] || { echo 'Save local source changes before updating.' >&2; exit 1; }
fi
existing=$(./scripts/production.sh ps -a -q bot)
if [ -n "$existing" ]; then
    running=$(docker inspect --format '{{.State.Running}}' "$existing")
    [ "$running" = true ] || { echo 'Existing bot is stopped. Start it and make a backup before deployment.' >&2; exit 1; }
    ./scripts/backup.sh
fi
mkdir -p deploy-history
if [ "$mode" = '--update' ]; then
    git rev-parse HEAD > deploy-history/previous-commit.txt
    git pull --ff-only origin main
    # Do not execute newly fetched shell code within an old in-flight shell script.
    exec ./scripts/deploy.sh
fi
python3 scripts/configure.py
./scripts/production.sh config --quiet
./scripts/production.sh build --pull
if ! ./scripts/production.sh up -d --wait --wait-timeout 180; then
    echo 'Deployment did not become healthy. Inspect: ./scripts/production.sh logs --tail=100 bot caddy' >&2
    echo 'Backup is in backups/. No automatic database rollback was attempted.' >&2
    exit 1
fi
./scripts/production.sh exec -T bot python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5).status == 200'
./scripts/production.sh ps
printf '%s\n' 'Deployment is running.' 'Set the RollyPay callback to https://YOUR-DOMAIN/webhooks/rollypay.' 'Verify HTTPS certificate issuance in Caddy logs and complete a test payment before real sales.'
