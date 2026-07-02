#!/usr/bin/env bash
# Deploy the `staging` branch to the STAGING environment (staging.podhost.club).
#
# Usage: scripts/deploy-staging.sh
#
# Runs from the staging checkout. Never touches prod or feature stacks.
set -euo pipefail

CHECKOUT=/root/good-360-bids-staging

cd "$CHECKOUT"

if [ ! -f .env ]; then
    echo "ERROR: $CHECKOUT/.env missing — see WORKFLOW.md (staging env bootstrap)" >&2
    exit 1
fi
grep -q '^ENABLE_AUTO_BUY=false' .env || {
    echo "ERROR: refusing to deploy — ENABLE_AUTO_BUY must be false in staging" >&2
    exit 1
}
grep -q '^ENABLE_URL_SCANNING=false' .env || {
    echo "ERROR: refusing to deploy — ENABLE_URL_SCANNING must be false in staging" >&2
    exit 1
}
grep -q '^ENABLE_NOTIFICATIONS=false' .env || {
    echo "ERROR: refusing to deploy — ENABLE_NOTIFICATIONS must be false in staging" >&2
    exit 1
}

git fetch origin
git checkout staging
git merge --ff-only origin/staging

docker compose -f docker-compose.staging.yml up -d --build
docker compose -f docker-compose.staging.yml ps
echo "Staging deployed: https://staging.podhost.club"
