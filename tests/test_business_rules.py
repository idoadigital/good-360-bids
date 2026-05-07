"""Tests for business rules: org filtering, truck/target matching, business hours."""
from datetime import datetime

import pytest
import pytz


# ---------------------------------------------------------------------------
# Business-hours rule (replicated from good360_watchdog.py to pin behavior)
# ---------------------------------------------------------------------------
def is_business_hours(dt: datetime) -> bool:
    """Mon–Fri 6am–11pm ET. Input must be timezone-aware ET datetime."""
    return dt.weekday() < 5 and 6 <= dt.hour < 23


ET = pytz.timezone("America/New_York")


@pytest.mark.parametrize(
    "year,month,day,hour,expected",
    [
        # Monday 10am — in window
        (2026, 4, 13, 10, True),
        # Monday 5:59am — before window
        (2026, 4, 13, 5, False),
        # Monday 6:00am — first minute of window
        (2026, 4, 13, 6, True),
        # Monday 10:59pm — last hour of window
        (2026, 4, 13, 22, True),
        # Monday 11:00pm — just past window
        (2026, 4, 13, 23, False),
        # Friday 8pm — in window
        (2026, 4, 17, 20, True),
        # Saturday 10am — weekend, out
        (2026, 4, 18, 10, False),
        # Sunday 10am — weekend, out
        (2026, 4, 19, 10, False),
    ],
)
def test_business_hours(year, month, day, hour, expected):
    dt = ET.localize(datetime(year, month, day, hour, 30))
    assert is_business_hours(dt) is expected


# ---------------------------------------------------------------------------
# Target truck matching (replicated from good360_monitor.get_org_for_truck)
# ---------------------------------------------------------------------------
def match_org_for_truck(truck_name: str, orgs: dict) -> tuple[str | None, dict | None]:
    for key, org in orgs.items():
        if not org.get("auto_buy") or org.get("paused") or org.get("cooldown_active"):
            continue
        for target in org.get("auto_buy_targets", []):
            if target.lower() in truck_name.lower():
                return key, org
    return None, None


@pytest.fixture
def orgs():
    return {
        "h4h": {
            "auto_buy": True,
            "paused": False,
            "cooldown_active": False,
            "auto_buy_targets": ["Amazon New Unsorted Truckload", "Amazon Variety Truckload"],
        },
        "paused_org": {
            "auto_buy": True,
            "paused": True,
            "cooldown_active": False,
            "auto_buy_targets": ["Amazon New Unsorted Truckload"],
        },
        "cooldown_org": {
            "auto_buy": True,
            "paused": False,
            "cooldown_active": True,
            "auto_buy_targets": ["Amazon New Unsorted Truckload"],
        },
        "autobuy_off": {
            "auto_buy": False,
            "auto_buy_targets": ["Amazon New Unsorted Truckload"],
        },
    }


def test_matches_active_org_on_target_name(orgs):
    key, _ = match_org_for_truck("Amazon New Unsorted Truckload #4712", orgs)
    assert key == "h4h"


def test_case_insensitive_match(orgs):
    key, _ = match_org_for_truck("amazon VARIETY truckload", orgs)
    assert key == "h4h"


def test_paused_org_never_matches(orgs):
    # Remove h4h so paused_org is the only target for "Unsorted"
    del orgs["h4h"]
    key, _ = match_org_for_truck("Amazon New Unsorted Truckload", orgs)
    assert key is None


def test_cooldown_org_never_matches(orgs):
    del orgs["h4h"]
    del orgs["paused_org"]
    key, _ = match_org_for_truck("Amazon New Unsorted Truckload", orgs)
    assert key is None


def test_autobuy_off_never_matches(orgs):
    del orgs["h4h"]
    del orgs["paused_org"]
    del orgs["cooldown_org"]
    key, _ = match_org_for_truck("Amazon New Unsorted Truckload", orgs)
    assert key is None


def test_non_target_truck_returns_none(orgs):
    key, _ = match_org_for_truck("Amazon Softlines Truckload", orgs)
    assert key is None


# ---------------------------------------------------------------------------
# Price cap rule — total > max_auto_pay triggers MANUAL, not auto-buy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "total,cap,expected",
    [
        (6399.99, 6400, "AUTO"),   # just under
        (6400.00, 6400, "AUTO"),   # exactly at cap (>, not >=)
        (6400.01, 6400, "MANUAL"),
        (9999, 6400, "MANUAL"),
        (0, 6400, "AUTO"),
    ],
)
def test_price_cap(total, cap, expected):
    result = "MANUAL" if total > cap else "AUTO"
    assert result == expected
