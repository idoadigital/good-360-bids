# Git History Scrub Plan — Phase 0

**Status:** DO NOT EXECUTE until secrets have been rotated and team has been notified.

## Why
Earlier commits contain live credit-card PANs/CVVs/expiries and plaintext passwords:
- `good360_orgs_master.json`
- `reviving_homes_config.json`

Deleting the files from the working tree does **not** remove them from history. Anyone who clones the repo (or already has a clone) still sees the secrets. A history rewrite with `git filter-repo` is required, followed by a force-push and invalidation of all existing clones.

## Prerequisites (human actions — cannot be automated)

1. **Rotate the Good360 account passwords** for every account listed in the JSON configs.
2. **Cancel & reissue every credit card** that appeared in the JSON (call the issuers). CVV exposure = card is compromised, regardless of whether "it was a private repo."
3. **Regenerate the Telegram bot token** (`/revoke` via @BotFather, issue a new one).
4. **Revoke and reissue the Gmail app password** (`iiqf rlwh bepm fcjf`) used by `intake_form/server.py` — Google Account → Security → App passwords → revoke and create new.
5. **Rotate the Mission Control API key** (`ecomsetter_good360_api_key_2026`) — replace hardcoded value with a generated secret from `openssl rand -hex 32`.
6. **Inform all current collaborators** of the force-push. Everyone must re-clone afterward.
7. **Back up the repo** (`cp -r Harden-good360-monitor Harden-good360-monitor.prescrub-backup`) before running anything below.

## Installation
```
brew install git-filter-repo     # or: pipx install git-filter-repo
```

## The scrub commands (run from the repo root)

```bash
# 1. Verify git-filter-repo is installed
git filter-repo --version

# 2. Create a fresh mirror to operate on
cd /Users/macmini
git clone --mirror https://github.com/Qompet/Harden-good360-monitor.git Harden-scrub.git
cd Harden-scrub.git

# 3. Purge sensitive files from all history
git filter-repo \
  --invert-paths \
  --path good360_orgs_master.json \
  --path reviving_homes_config.json \
  --path intake_form/review_queue.json \
  --path-glob 'good360_roster/config_*.json' \
  --path-glob 'test_reviving_login*.py' \
  --path-glob '*.png' \
  --path-glob '*.log' \
  --path-glob '*.bak*' \
  --path-glob '*_old.py' \
  --path-glob '*_fixed.py' \
  --path-glob '*_backup*.py' \
  --path-glob '*.broken*' \
  --path-glob '*.corrupted*' \
  --path-glob '*.backup_*'

# 4. (Optional) Also replace any residual literal secret strings in remaining files
cat > /tmp/replacements.txt <<'EOF'
Lovebird2@==>REDACTED
Berneitha2026==>REDACTED
Keephope8live!==>REDACTED
4427322547206651==>REDACTED
4036230406667421==>REDACTED
8434926548:AAEF9IdjbjPWTLtR4Lxt_2-ErZjQS38gZzI==>REDACTED
ecomsetter_good360_api_key_2026==>REDACTED
iiqf rlwh bepm fcjf==>REDACTED
swmkfdyprgtsqvny==>REDACTED
EOF
git filter-repo --replace-text /tmp/replacements.txt --force

# 5. Push the rewritten history back (DESTRUCTIVE — force-push)
git remote add origin https://github.com/Qompet/Harden-good360-monitor.git
git push --force --all
git push --force --tags

# 6. On GitHub: Settings → Danger Zone → ensure no forks exist.
#    Visit: https://github.com/Qompet/Harden-good360-monitor/network/members
#    If forks exist, contact GitHub support to have them purged —
#    force-push does NOT remove blobs from forks.

# 7. Every collaborator MUST delete their local clone and re-clone.
#    Old clones still contain the secrets in their reflog.
```

## Verification after scrub

```bash
cd /tmp && git clone https://github.com/Qompet/Harden-good360-monitor.git verify && cd verify
git log --all --oneline | wc -l              # Should be lower than pre-scrub
git grep -i "lovebird2"   $(git rev-list --all) || echo "clean"
git grep -E "4427322547206651|4036230406667421" $(git rev-list --all) || echo "clean"
```

Both greps should print `clean`.

## Assume compromised regardless
Even after a successful scrub, treat every secret previously in git as **publicly known**. The scrub reduces future exposure; it does not undo past exposure. Rotation is the real fix.
