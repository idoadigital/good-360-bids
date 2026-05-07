"""Tests for deadman_switch — the independent probe."""
import importlib
import json
from datetime import UTC, datetime, timedelta


def _fresh_hb(age_minutes=0):
    ts = datetime.now(UTC) - timedelta(minutes=age_minutes)
    return {"last_success": ts.isoformat()}


def test_heartbeat_age_returns_none_for_missing():
    import deadman_switch
    assert deadman_switch.heartbeat_age(None) is None
    assert deadman_switch.heartbeat_age({}) is None
    assert deadman_switch.heartbeat_age({"last_success": "not-a-date"}) is None


def test_heartbeat_age_returns_delta():
    import deadman_switch
    age = deadman_switch.heartbeat_age(_fresh_hb(age_minutes=3))
    assert timedelta(minutes=2, seconds=30) < age < timedelta(minutes=4)


def test_main_alerts_on_missing_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("DEADMAN_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "no-such-file.json"))
    import deadman_switch
    importlib.reload(deadman_switch)

    sent = []
    monkeypatch.setattr(deadman_switch, "send_alert", lambda m: sent.append(m))

    rc = deadman_switch.main()
    assert rc == 1
    assert any("TRIPPED" in m for m in sent)

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["alerted"] is True


def test_main_clears_when_heartbeat_returns(tmp_path, monkeypatch):
    hb_path = tmp_path / "hb.json"
    hb_path.write_text(json.dumps(_fresh_hb(age_minutes=0)))
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"alerted": True, "since": "x"}))

    monkeypatch.setenv("DEADMAN_STATE", str(state_path))
    monkeypatch.setenv("HEARTBEAT_FILE", str(hb_path))
    import deadman_switch
    importlib.reload(deadman_switch)

    sent = []
    monkeypatch.setattr(deadman_switch, "send_alert", lambda m: sent.append(m))

    rc = deadman_switch.main()
    assert rc == 0
    assert any("CLEARED" in m for m in sent)

    state = json.loads(state_path.read_text())
    assert state["alerted"] is False


def test_main_quiet_when_healthy_and_not_previously_alerted(tmp_path, monkeypatch):
    hb_path = tmp_path / "hb.json"
    hb_path.write_text(json.dumps(_fresh_hb(age_minutes=0)))
    monkeypatch.setenv("DEADMAN_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("HEARTBEAT_FILE", str(hb_path))
    import deadman_switch
    importlib.reload(deadman_switch)

    sent = []
    monkeypatch.setattr(deadman_switch, "send_alert", lambda m: sent.append(m))
    rc = deadman_switch.main()
    assert rc == 0
    assert sent == []
