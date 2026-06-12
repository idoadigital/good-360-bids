# Dynamic Buyer History — Design (2026-06-12)

Approved by operator 2026-06-12 (~05:00 UTC), Approach A: extend existing
purchase records; no new tables.

## Goal

Purchase records become living documents: they carry a post-purchase order
status that can change (delivered / canceled / refunded), auto-sync from
Good360's own Order History, attach proof automatically, and expose verified
spend per order and per customer.

## Non-goals (v1)

- No separate `orders` table (Approach B) — revisit if the model strains.
- No manual file uploads as proof (auto evidence only, per operator).
- No fresh credential logins by the auto-sync — it reuses the daemon's saved
  sessions only; when a session is dead it alerts and skips (lockout caution).
  The "Sync now" button after a fresh purchase always has a live session.
- No global spend stat (operator chose per-customer + per-order only).

## Data model (roster.db `purchase_attempts`, migrated columns)

| column | type | meaning |
|---|---|---|
| `order_status` | TEXT | `approved` / `delivered` / `canceled` / `refunded` (NULL = pre-feature row) |
| `order_status_source` | TEXT | `auto` (verifier) or `manual` (operator button) |
| `order_status_updated_at` | TEXT | sqlite datetime |
| `proof` | TEXT JSON | `{order_id, capture_path, screenshots: [], verifications: [{at, status, admin_fee, screenshot}]}` |

Migration lives in `quickbeed_roster_sync.ensure_roster_initialized()`
(same pattern as `manual_rank`).

**Manual wins:** the verifier never overwrites a row whose
`order_status_source='manual'`. Operator buttons always may.

## Order verifier (`missioncontrol/order_verifier.py`)

- Input: one customer (or all customers having successful attempts with a
  numeric confirmation number in the last 90 days).
- Reuses Playwright `storage_state` from `workdir/browser_data/qb_<org_id>/`.
  Headless context → `/marketplace/my-account/orders` → parse table rows
  (date, Order ID, admin fee, status, location) → screenshot saved to
  `workdir/browser_screenshots/verify_<org>_<ts>.png`.
- If the page shows a login wall: Telegram the operator, mark the customer
  skipped, do NOT attempt a login.
- Match `confirmation_number == Order ID`: set `order_total` (admin fee),
  `order_status` (site status lowercased; unknown values kept verbatim),
  `order_status_source='auto'`, append a verification entry to `proof`.
- State/locking: `workdir/order_verifier_state.json` records per-customer
  last-run; a lock file prevents concurrent runs (gunicorn has 3 workers).

## Triggers

- **Daily:** the monitor's main loop (single process) calls the verifier when
  `state.last_full_run` is >24h old. Time-gated, non-blocking failure.
- **Manual:** `POST /api/admin/customers/<id>/orders/sync` runs it for one
  customer in a background thread (audited).

## Endpoints

- `PATCH /api/admin/purchases/roster/<attempt_id>/order-status`
  body `{status: approved|delivered|canceled|refunded}` → sets manual status,
  writes `admin_audit` row. 400 on unknown status; roster source only (legacy
  rows have no lifecycle).
- `POST /api/admin/customers/<id>/orders/sync` → `{started: true}`;
  result lands in the purchase rows (UI refetches).

## Proof auto-capture at purchase time

`good360_autobuy_v2` success path seeds `proof` with the order/evidence id,
the daemon capture JSON path, and the step-screenshot paths, and sets
`order_status='approved'` + `source='auto'` for the new row.

## UI (admin.js / admin.html)

- Buyer history rows (customer page) and Purchases-tab expanded rows gain:
  status chip (color per status), three buttons (Delivered / Canceled /
  Refunded) calling the PATCH, and a "Sync now" button on the customer's
  purchases header.
- Per-order verified total already renders via `total`; per-customer
  lifetime spend renders from the endpoint's `total_spend` (now that totals
  are populated by the verifier).

## Testing

- Unit (isolated container, temp DBs): migration adds columns; status
  mapping + match logic; manual-wins rule; PATCH endpoint sets/refuses
  correctly. Playwright parsing exercised live (reviving homes has a fresh
  session + a real Approved order to verify against).
- Live acceptance: run verifier for reviving homes → order 100272562 row
  gains `order_status=approved`, `order_total=5837.12` (already backfilled),
  and a verification proof entry.

## Risks

- Saved sessions expire → verifier degrades to alert+skip (by design).
- Good360 may rename statuses → unknown statuses stored verbatim, surfaced
  in UI as-is.
- Screenshots already on disk contain card PANs (separate finding, 2026-06-12)
  — verifier screenshots contain no card data (orders page only).
