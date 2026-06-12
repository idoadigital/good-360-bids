"""Failing-first test: re-alert cadence for still-available trucks.
_should_realert(state, name, now_ts) -> True when the last alert for a
truck is older than AVAILABLE_REALERT_SECONDS (default 30)."""
import os, sys
os.environ["DASHBOARD_DB"] = "/tmp/t.db"; os.environ["WORKDIR"] = "/tmp"
import good360_monitor as mon

failures = []
if not hasattr(mon, "_should_realert"):
    failures.append("_should_realert does not exist")
else:
    NOW = 1_000_000.0
    if mon._should_realert({}, "TruckA", NOW) is not True:
        failures.append("no alert_times recorded -> must re-alert")
    st = {"alert_times": {"TruckA": NOW - 5}}
    if mon._should_realert(st, "TruckA", NOW) is not False:
        failures.append("alerted 5s ago -> must NOT re-alert")
    st = {"alert_times": {"TruckA": NOW - 31}}
    if mon._should_realert(st, "TruckA", NOW) is not True:
        failures.append("alerted 31s ago -> must re-alert")
import inspect
src = inspect.getsource(mon.main)
if "_should_realert" not in src:
    failures.append("main() does not use _should_realert (re-alert not wired)")
if failures:
    print("FAIL:"); [print("  -", f) for f in failures]; sys.exit(1)
print("PASS: 30s re-alert logic present and wired")
