"""Tests for healthcheck.py — the Real Health Check (the postmortem fix)."""
import json
from datetime import UTC, datetime, timedelta

import healthcheck


def _write_heartbeat(path, iso_ts, field="last_success"):
    path.write_text(json.dumps({field: iso_ts}))


def test_heartbeat_missing_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(healthcheck, "HEARTBEAT_FILE", tmp_path / "nope.json")
    ok, msg = healthcheck.check_heartbeat()
    assert not ok
    assert "missing" in msg


def test_heartbeat_fresh_passes(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    _write_heartbeat(hb, datetime.now(UTC).isoformat())
    monkeypatch.setattr(healthcheck, "HEARTBEAT_FILE", hb)
    ok, _ = healthcheck.check_heartbeat()
    assert ok


def test_heartbeat_stale_fails(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    stale = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    _write_heartbeat(hb, stale)
    monkeypatch.setattr(healthcheck, "HEARTBEAT_FILE", hb)
    monkeypatch.setattr(healthcheck, "MAX_STALE_MINUTES", 5)
    ok, msg = healthcheck.check_heartbeat()
    assert not ok
    assert "ago" in msg


def test_heartbeat_accepts_last_scan_or_last_success(tmp_path, monkeypatch):
    for field in ("last_scan", "last_success"):
        hb = tmp_path / f"hb_{field}.json"
        _write_heartbeat(hb, datetime.now(UTC).isoformat(), field=field)
        monkeypatch.setattr(healthcheck, "HEARTBEAT_FILE", hb)
        ok, _ = healthcheck.check_heartbeat()
        assert ok, f"should accept {field}"


def test_heartbeat_malformed_fails(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    hb.write_text("{not json")
    monkeypatch.setattr(healthcheck, "HEARTBEAT_FILE", hb)
    ok, msg = healthcheck.check_heartbeat()
    assert not ok
    assert "unreadable" in msg


def test_check_orgs_empty_when_no_env(clean_env, tmp_path, monkeypatch):
    # With no GOOD360_* env set, config.load_orgs drops every org.
    ok, msg = healthcheck.check_orgs_configured()
    assert not ok
    assert "no orgs" in msg
