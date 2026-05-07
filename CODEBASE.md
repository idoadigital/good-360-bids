# Codebase Map — Harden-good360-monitor

Quick reference for "what runs what." Updated 2026-04-13.

## Live entry points (what `docker-compose.yml` starts)

| Service          | Script                       | Port | Purpose                              |
|------------------|------------------------------|------|--------------------------------------|
| `monitor`        | `good360_monitor.py`         | —    | Scan loop; writes heartbeat          |
| `daemon`         | `good360_daemon.py`          | 5002 | Persistent browser + `/checkout` API |
| `watchdog`       | `good360_watchdog.py`        | —    | Detects stale scans → Telegram alert |
| `telegram-bot`   | `good360_telegram_bot.py`    | —    | `/status /pause /resume …` commands  |
| `missioncontrol` | `missioncontrol/server_v2.py`| 5001 | Dashboard API (v2 is canonical)      |
| `intake`         | `intake_form/server.py`      | 5000 | Public intake form → Telegram/email  |
| (on-demand)      | `good360_report.py`          | —    | Daily digest email                   |

## Two parallel call paths — not yet consolidated

### Path A — legacy (single-org, Hope 4 Humanity)
```
good360_monitor.py  →  subprocess: good360_autobuy.py
```
`good360_autobuy.py` is a 386-line script with Hope 4 Humanity semantics baked in (max price, card from `CARD_HOPE4HUMANITY_*` env). This is what the live cron currently drives.

### Path B — roster (multi-org, v2)
```
good360_roster/roster_orchestrator.py  →  good360_roster/good360_autobuy_v2.py
```
v2 is 884 LOC, org-parameterized, with Single-Purchase Lock and credential-vault integration. `roster_orchestrator.py` and `health_checker.py` import `attempt_purchase`, `verify_org_credentials`, `check_master_card_available` from v2.

### Why both still exist
The monitor was never wired to call v2. Migrating the monitor to call `autobuy_v2.attempt_purchase(org_config)` instead of `subprocess(good360_autobuy.py)` is the cleanest way to retire the legacy path — but it's a behavior-visible change (purchase path) and deserves its own PR + dry-run on staging. Tracked as Phase 2b.

## Removed in Phase 2
- `missioncontrol/server.py` (superseded by `server_v2.py`)
- `llm_context.json`, `llm_settings.json` (dead; referenced a deprecated OpenAI model, no import chain)
- `intake_config.yml`, `intake_config_clean.yml` (only `intake_config_final.yml` kept as the canonical Cloudflare tunnel config)
- `intake_ingress.yml` (unused)
- `test_error_handling.py`, `test_login_flow.py`, `test_reviving_login{,_v2,_v3}.py` (ad-hoc probes, not real tests — belonged in a scratch dir)

## Path conventions
All file paths now derive from `WORKDIR` (defaults to `/a0/usr/workdir` for backward compat; Docker sets it to `/app/workdir`). Search the repo for a literal `"/a0/usr/workdir"` — every remaining hit should be a default value in an `os.environ.get("WORKDIR", ...)` call.

## Still open
- Retire Path A (monitor → v2) — Phase 2b
- Move runtime mutation of org pause/cooldown state off of the example JSON and into a separate state file — currently the example template is treated as both config and state, which is brittle.
