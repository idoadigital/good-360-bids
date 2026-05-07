"""Shared pytest fixtures."""
import os
import sys
from pathlib import Path

import pytest

# Make repo root importable so `import config` works in tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def clean_env(monkeypatch):
    """Clear every GOOD360_* / CARD_* / TELEGRAM_* var so tests start blank."""
    for key in list(os.environ):
        if key.startswith(("GOOD360_", "CARD_", "TELEGRAM_", "SMTP_", "ALERT_EMAIL_", "MISSIONCONTROL_")):
            monkeypatch.delenv(key, raising=False)
    yield monkeypatch
