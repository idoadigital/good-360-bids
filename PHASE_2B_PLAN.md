# Phase 2b: retire legacy autobuy path

## Current state
Two parallel purchase paths exist. See `CODEBASE.md` for the full map.

- **Legacy (live):** `good360_monitor.py` detects truck → `subprocess([sys.executable, AUTOBUY_SCRIPT, ...])` → `good360_autobuy.py` (single-org, Hope 4 Humanity).
- **Roster (partially-wired):** `roster_orchestrator.py` pulls events from a SQLite queue → imports `attempt_purchase(org_id, event_id)` from `good360_roster/good360_autobuy_v2.py` (multi-org, credential-parameterized).

## Why this isn't a drop-in swap
The two functions have *different contracts*:

| | Legacy | v2 |
|---|---|---|
| Input | `truck_name`, `truck_url`, `admin_fee` (stdin via argv) | `org_id`, `event_id` (DB row refs) |
| Process model | one-shot subprocess | in-process function call |
| Output | exit code `0/1/2/3/4/5` + stdout last line | dict `{status, event_id}` |
| State storage | file flags (`good360_paused_*.flag`) | SQLite tables (`truck_events`, `purchase_attempts`) |
| Credentials | reads from `good360_checkout_config.json` | reads from `roster.db` (Fernet-encrypted) |

Retiring legacy means the monitor has to **create an event row** (so v2 has an `event_id` to reference) *before* calling `attempt_purchase`. That's new DB-write behavior in the hot path.

## Proposed sequence

### Step 1 — build a thin dispatcher (shim)
New module `autobuy_dispatch.py`:

```python
def dispatch(truck_name, truck_url, admin_fee, org_key) -> tuple[str, str]:
    """Return (status, message). Status uses the legacy string vocab."""
    backend = os.environ.get("AUTOBUY_BACKEND", "legacy")
    if backend == "roster":
        return _dispatch_roster(truck_name, truck_url, org_key)
    return _dispatch_legacy(truck_name, truck_url, admin_fee, org_key)
```

Replace the `subprocess.run([... AUTOBUY_SCRIPT ...])` block in `good360_monitor.py` with `dispatch(...)`. Default stays `legacy`. No behavior change.

### Step 2 — write the roster adapter
Inside `_dispatch_roster`:
1. Open the roster DB.
2. `INSERT INTO truck_events` with the scraped truck metadata → get `event_id`.
3. Call `attempt_purchase(org_id, event_id)`.
4. Map the v2 dict status back to the legacy string (`SUCCESS`/`FAILED`/`MISSED`/`MANUAL`/`COOLDOWN`/`LOCKED`).

### Step 3 — dry-run on staging
Flip `AUTOBUY_BACKEND=roster` on a staging container that points to a **staging Good360 account** (not production). Monitor real truck alerts for a week. Watch:
- Purchase attempts land in `purchase_attempts` table
- No double-buys
- Cooldowns apply correctly
- Alert emails still fire

### Step 4 — production flip
One-shot: change the prod compose env to `AUTOBUY_BACKEND=roster`. Do NOT remove the legacy code in the same change — keep the fallback for ~2 weeks.

### Step 5 — delete legacy
Once two weeks of clean operation, delete:
- `good360_autobuy.py` (root script)
- `AUTOBUY_SCRIPT` constant in `good360_monitor.py`
- `_dispatch_legacy` in the shim (or delete the shim entirely if nothing else branches on it)

## Blockers before starting
1. **Staging environment.** Right now there's one prod Good360 account; flipping `AUTOBUY_BACKEND=roster` against it is the dry-run *and* the live test. Don't.
2. **Encrypted credentials in roster.db.** v2 reads from `vault.py` / Fernet. The Harden repo's `.env` carries plaintext card data via env vars. Either teach v2 to read env (easier, less secure), or write an env→vault loader (slower, more correct).
3. **Event schema alignment.** The monitor scrapes what v2's event table expects? Check `good360_autobuy_v2.py`'s `attempt_purchase` → which DB fields it reads.

## Risks
- **Double-purchase.** If the monitor's dispatcher fires *and* the roster bridge (`good360_bridge.py`) also picks up the truck from its own poll loop, both could try to buy. Mitigation: retire the bridge (`good360_roster/good360_bridge.py`) in the same change, or make v2's Single-Purchase Lock cover both entry points.
- **DB contention.** SQLite on a shared volume + two writers (the monitor via dispatch + the orchestrator) = `database is locked` errors. Switch to `WAL` mode or move to Postgres.

## Definition of done
- `good360_autobuy.py` (legacy) is deleted.
- `docker-compose.yml` has `AUTOBUY_BACKEND=roster` (or the flag is gone).
- Tests cover the dispatcher's status-code translation.
- The `RUN_LOG` / `audit_log` entries for the first roster-backed purchase have been verified by hand.
