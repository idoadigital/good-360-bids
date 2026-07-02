#!/usr/bin/env bash
# Deploy the `main` branch to PRODUCTION (podhost.club).
#
# Usage: scripts/deploy-prod.sh
#
# OPERATOR-RUN ONLY. This recreates production containers (brief blip while
# each service restarts; caddy is recreated last only if its config changed).
# Auto-buy and scanning are LIVE here — deploy only what staging has verified.
set -euo pipefail

CHECKOUT=/root/good-360-bids

cd "$CHECKOUT"

if [ ! -f .env ]; then
    echo "ERROR: $CHECKOUT/.env missing" >&2
    exit 1
fi
# Prod must NOT have the kill-switches set to false.
if grep -qE '^ENABLE_(AUTO_BUY|URL_SCANNING|NOTIFICATIONS)=false' .env; then
    echo "ERROR: prod .env has an ENABLE_* kill-switch set to false — fix before deploying" >&2
    exit 1
fi

git fetch origin
git checkout main
git merge --ff-only origin/main

docker compose up -d --build
docker compose ps
echo "Production deployed: https://podhost.club"
