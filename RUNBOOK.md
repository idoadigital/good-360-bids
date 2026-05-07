# Runbooks

Response procedures for the failure modes the April 13, 2026 postmortem identified. Each runbook assumes Docker Compose deployment (see `docker-compose.yml`). Adapt commands if running bare-metal.

---

## RB-1: Dead-man's switch tripped ("DEAD-MAN'S SWITCH TRIPPED" Telegram)

**Meaning:** The independent probe (`deadman_switch.py`, running on a different host) could not confirm a fresh heartbeat. The main system is silent — it may be crashed, unreachable, or the heartbeat endpoint is broken.

**Check, in order:**

1. **Is the main host reachable?**
   ```
   ssh <main-host> uptime
   ```
   If no: it's a host/network problem. Check the cloud console, power, etc.

2. **Is the `monitor` container running?**
   ```
   ssh <main-host>
   docker compose ps
   ```
   Expect `monitor` state = `running (healthy)`. If not:
   ```
   docker compose logs --tail=100 monitor
   docker compose up -d monitor
   ```

3. **Is playwright broken?** Run the real health check directly:
   ```
   docker compose exec monitor python /app/healthcheck.py --verbose
   ```
   If `playwright: FAIL` — the chromium binary or venv is gone. Rebuild:
   ```
   docker compose build --no-cache monitor
   docker compose up -d
   ```

4. **Is the heartbeat file writable?**
   ```
   docker compose exec monitor ls -la /app/workdir/good360_heartbeat.json
   ```
   If missing or stale, the scan loop isn't reaching the heartbeat write.

5. **Post-incident:** once recovered, you'll get an "DEAD-MAN'S SWITCH CLEARED" alert. Append a note to `POSTMORTEM_<date>.md`.

---

## RB-2: Python venv or playwright wiped (April 13 root cause)

**Symptoms:** Monitor container keeps restarting. Logs show `ModuleNotFoundError: playwright` or `chromium binary not found`.

**Why this shouldn't happen anymore:** The Docker image ships with `playwright==1.58.0` and the matched chromium. If a fresh `docker compose up` still fails:

1. Confirm you're on the right image:
   ```
   docker images | grep good360-monitor
   ```
2. Force a rebuild (not a cache hit):
   ```
   docker compose build --no-cache
   docker compose up -d
   ```
3. If that still fails, the base image may have moved. Pin explicitly in `Dockerfile` (already done: `mcr.microsoft.com/playwright/python:v1.58.0-jammy`).

---

## RB-3: Cron file deleted (April 13 secondary cause)

**Symptoms:** Everything "looks" fine but no scans happen.

**Why this shouldn't happen anymore:** We no longer rely on host cron. The `monitor` service runs as a long-lived container with `restart: always`. Scheduling is inside the Python loop.

If you see this regress, verify nobody reverted to host cron:
```
crontab -l | grep good360
ls /etc/cron.d/ | grep good360
```
Both should be empty on a docker-deployed host.

---

## RB-4: Telegram bot not responding to /status

**Check:**
```
docker compose logs --tail=50 telegram-bot
```

Common causes:
- **Token rotated but not reloaded:** `docker compose restart telegram-bot`
- **Old `asyncio` event-loop bug (the "duplicate main()" issue):** if you see `RuntimeError: Event loop is closed`, check `good360_telegram_bot.py` for duplicate `Application.builder()` blocks. There should be exactly one.
- **Wrong chat ID:** send a test message from the bot to the chat, then call `getUpdates` to confirm the ID matches `TELEGRAM_OPERATOR_CHAT_ID` in `.env`.

---

## RB-5: `MISSIONCONTROL_API_KEY is not set — refuse to start` on missioncontrol boot

The v2 server hard-fails if the key is empty. Populate it in `.env` (generate with `openssl rand -hex 32`) and:
```
docker compose up -d missioncontrol
```

---

## RB-6: Auto-buy placed a duplicate or wrong purchase

1. **Pause all autobuy immediately:**
   ```
   curl -X POST -H "X-API-Key: $MISSIONCONTROL_API_KEY" \
        https://<host>/api/pause \
        -d '{"reason":"incident investigation"}'
   ```
2. **Read the audit log:**
   ```
   docker compose exec monitor cat /app/workdir/audit/audit-$(date +%F).jsonl | jq
   ```
   Every purchase_attempt is here with timestamp, org, truck, total, status.
3. **Contact Good360 support** with the reference number to request cancellation (if within their window).
4. **Write a postmortem** (`POSTMORTEM_<YYYY-MM-DD>.md`) identifying the root cause before resuming auto-buy.

---

## RB-7: Secret leaked (another one appears in a code search / gitleaks hit)

1. **Rotate the leaked secret** immediately (same process as the initial Phase 0 rotation — see `SCRUB_PLAN.md`).
2. **Add the literal to `SCRUB_PLAN.md`** so the next history rewrite includes it.
3. **Do NOT scrub history for a single leak** unless it's material — repeated scrubs train collaborators to accept unfamiliar force-pushes, which is its own risk. Batch leaks and scrub deliberately.
4. **Add a `gitleaks` ignore or regex** if the false-positive rate climbs.

---

## Health check reference
```
docker compose exec monitor python /app/healthcheck.py --verbose
```
Prints each of: heartbeat freshness, playwright launch, at least-one-org-configured. Non-zero exit = Docker will mark unhealthy and restart.
