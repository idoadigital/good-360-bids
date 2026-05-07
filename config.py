"""Central config loader.

Loads `.env` + a JSON template (with `${VAR}` placeholders) and returns a dict
with placeholders substituted from environment. Orgs whose required secrets are
unset are dropped so the rest of the system keeps running.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv optional: env vars can also come from the shell/container.
    pass

_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
_REPO_ROOT = Path(__file__).resolve().parent

_REQUIRED_ORG_FIELDS = ("good360_email", "good360_password")


def _substitute(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")
        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v) for v in value]
    return value


def _org_has_required_secrets(org: dict) -> bool:
    return all(org.get(f) for f in _REQUIRED_ORG_FIELDS)


def load_orgs(template_path: str | Path = "good360_orgs_master.example.json") -> dict:
    """Return orgs keyed by org_id, with env placeholders resolved.

    Orgs missing required secrets are excluded.
    """
    path = Path(template_path)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    raw = json.loads(path.read_text())
    resolved = _substitute(raw)
    return {
        org_id: org
        for org_id, org in resolved.items()
        if isinstance(org, dict) and _org_has_required_secrets(org)
    }


def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return val or ""
