#!/usr/bin/env bash
# Deploy a feature branch to the FEATURE environment (feature.podhost.club).
#
# Usage: scripts/deploy-feature.sh <branch>
#   e.g. scripts/deploy-feature.sh feature/my-change
#
# Runs from the feature checkout. Never touches prod or staging stacks.
set -euo pipefail

CHECKOUT=/root/good-360-bids-feature
BRANCH="${1:?usage: deploy-feature.sh <branch>}"

cd "$CHECKOUT"

if [ ! -f .env ]; then
    echo "ERROR: $CHECKOUT/.env missing — see WORKFLOW.md (feature env bootstrap)" >&2
    exit 1
fi
grep -q '^ENABLE_AUTO_BUY=false' .env || {
    echo "ERROR: refusing to deploy — ENABLE_AUTO_BUY must be false in the feature env" >&2
    exit 1
}
grep -q '^ENABLE_URL_SCANNING=false' .env || {
    echo "ERROR: refusing to deploy — ENABLE_URL_SCANNING must be false in the feature env" >&2
    exit 1
}

git fetch origin
git checkout "$BRANCH"
# Fast-forward to origin if the branch exists there (local-only branches are fine too).
git rev-parse --verify -q "origin/$BRANCH" >/dev/null && git merge --ff-only "origin/$BRANCH"

docker compose -f docker-compose.feature.yml up -d --build
docker compose -f docker-compose.feature.yml ps
echo "Feature env deployed: https://feature.podhost.club"
