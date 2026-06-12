#!/usr/bin/env python3
"""Roster parity audit — the operator's 'total test'.

Answers three questions, live, read-only:
  1. WHO is the engine's next-up org right now (the exact pick a truck
     trigger would execute)?
  2. Would the purchase-point approval gate allow that pick?
  3. For EVERY org: does the engine's eligibility match the dashboard's
     intent (status + rotation + cooldown)? Any mismatch is the class of
     bug that mis-routed picks on 2026-06-12 (Di'Marco incident).

Run (missioncontrol mounts the repo):
  docker exec -i good-360-bids-missioncontrol-1 python /root/good-360-bids/scripts/roster_parity_check.py
Exits 1 on any parity mismatch so it can gate automation.

Note: question 1's pick call also releases any expired cooldowns
(point-of-pick sweep) — running this audit self-heals stale state.
"""
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/app/missioncontrol")

from queue_manager import find_next_available_org  # noqa: E402
from good360_autobuy_v2 import _live_purchase_approval_blocker  # noqa: E402
from db import get_conn  # noqa: E402

pick = find_next_available_org()
print(f"ENGINE NEXT-UP: {pick['org_name'] if pick else None}")
if pick:
    blocker = _live_purchase_approval_blocker(pick["id"])
    print(f"APPROVAL GATE:  {'APPROVED' if blocker is None else 'BLOCKED: ' + blocker}")
print()

rc = sqlite3.connect("/app/good360_roster/db/roster.db")
rc.row_factory = sqlite3.Row
roster = {r["quickbeed_customer_id"]: r for r in rc.execute("SELECT * FROM nonprofits")}
rc.close()

now_dash = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
now_iso = datetime.utcnow().isoformat()
mismatches = 0
print(f"{'org':32} {'dash wants':>10} {'engine has':>10}  verdict")
with get_conn() as c:
    for cust in c.execute(
        "SELECT * FROM customers "
        "ORDER BY (manual_queue_position IS NULL), manual_queue_position"
    ):
        n = roster.get(cust["id"])
        dash = (cust["status"] == "active"
                and int(cust["in_rotation"] or 0) == 1
                and (cust["cooldown_until"] is None or cust["cooldown_until"] < now_dash))
        eng = bool(n and n["status"] == "active" and n["auto_buy_global"] == 1
                   and (n["cooldown_until"] is None or n["cooldown_until"] < now_iso))
        ok = dash == eng
        if not ok:
            mismatches += 1
        print(f"{(cust['organization_name'] or '?')[:32]:32} "
              f"{str(dash):>10} {str(eng):>10}  {'OK' if ok else '** MISMATCH **'}")

print()
if mismatches:
    print(f"PARITY FAILED: {mismatches} mismatch(es) — engine disagrees with dashboard")
    sys.exit(1)
print("PARITY PERFECT: engine matches dashboard for all orgs")
