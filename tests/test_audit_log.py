"""Tests for audit_log — the money-trail ledger."""
import importlib
import json


def test_audit_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path))
    import audit_log
    importlib.reload(audit_log)  # pick up the new env

    audit_log.audit("purchase_attempt", org_id="h4h", total=6399.00, status="SUCCESS")
    audit_log.audit("purchase_attempt", org_id="h4h", total=1200.00, status="FAILED")

    files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(files) == 1

    lines = files[0].read_text().strip().splitlines()
    assert len(lines) == 2
    entries = [json.loads(l) for l in lines]

    assert entries[0]["event"] == "purchase_attempt"
    assert entries[0]["org_id"] == "h4h"
    assert entries[0]["status"] == "SUCCESS"
    assert "ts" in entries[0] and "host" in entries[0] and "pid" in entries[0]


def test_audit_never_raises(tmp_path, monkeypatch):
    """Audit calls must NEVER break the caller, even if disk is unwritable."""
    monkeypatch.setenv("AUDIT_LOG_DIR", "/nonexistent/cannot-create-this/path")
    import audit_log
    importlib.reload(audit_log)

    # Should swallow any OSError and stay quiet (prints to stderr instead).
    audit_log.audit("event_that_would_normally_fail", x=1)
