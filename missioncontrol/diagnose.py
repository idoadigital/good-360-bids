"""AI-driven diagnosis for failed autobuy attempts, via OpenRouter.

When an operator expands a failed purchase row in the dashboard, the UI
hits an endpoint that calls `diagnose_failure(...)`. We pass the current
failure record plus a handful of similar past failures and ask the
configured model (default: Claude Haiku 4.5 via OpenRouter) for a short,
actionable explanation.

Why OpenRouter rather than the Anthropic SDK directly:
  • The operator manages the API key from the dashboard's Settings panel
    instead of editing .env and recreating containers.
  • One gateway exposes Claude, GPT, Gemini, etc., so swapping models is
    a settings change, not a code edit.
  • httpx is already a transitive dep — we don't introduce the anthropic
    or openai SDK packages just for one short JSON call.

Concurrency: httpx.Client is thread-safe; we keep one module-level
instance and reuse it across requests. The lock guards first-init only.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import httpx

import secrets_store
from db import get_conn

logger = logging.getLogger("missioncontrol.diagnose")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
MAX_OUTPUT_TOKENS = 256
REQUEST_TIMEOUT_S = 30.0

# OpenRouter populates rankings/analytics from these two headers. Not
# required for the call to work; they identify *this* deployment in
# OpenRouter's leaderboards.
_OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "https://podhost.club/")
_OPENROUTER_TITLE   = os.environ.get("OPENROUTER_TITLE",   "good-360-bids missioncontrol")

_client_lock = threading.Lock()
_client: httpx.Client | None = None


def _http() -> httpx.Client:
    """Lazy, thread-safe httpx client singleton with sane connection
    pooling defaults. One TCP+TLS connection is reused across operators."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        _client = httpx.Client(
            base_url=OPENROUTER_BASE_URL,
            timeout=REQUEST_TIMEOUT_S,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        return _client


def _read_setting(key: str, default: str = "") -> str:
    """Read one value from the encrypted settings store. Returns `default`
    on any failure (missing row, decrypt error, schema not yet migrated)
    so a broken settings layer never crashes a request."""
    try:
        with get_conn() as c:
            row = c.execute(
                "SELECT value_enc FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        return (secrets_store.decrypt(row["value_enc"]) or default).strip()
    except Exception:
        return default


SYSTEM_PROMPT = """You diagnose failed Good360 autobuy attempts for a Playwright-driven truck-purchasing system.

The system runs in two paths: a legacy script (`good360_autobuy.py`) and a roster-driven v2 path (`good360_autobuy_v2.py`). Failures land in a SQLite history with structured fields: `status`, `reason`, `error_message`, `truck`, `org`, `final_url`, `confirmation_number`, plus a free-text `detail`.

Your job: given the current failure plus 0–10 similar past failures (same org or same reason), return a short diagnosis and one concrete operator action.

Output strict JSON with exactly these keys:
{
  "diagnosis": "<2 to 3 sentences explaining the likely root cause, citing patterns from the history when relevant>",
  "suggested_action": "<one sentence telling the operator what to do next, ordered by what's likely to fix the most cases>"
}

Rules:
- Be specific. Reference the actual `reason` / `error_message` text rather than restating that the attempt failed.
- When 2+ recent failures share the same `reason`, mention the pattern (e.g. "3 of the last 4 failures hit the same shipping dropdown — Good360 likely renamed an option").
- Suggested actions should be concrete: "check the truck on Good360 manually", "edit the shipping option matcher in good360_autobuy.py:489", "verify the customer's card hasn't expired", etc. Avoid vague verbs like "investigate" or "review".
- Output JSON only. No markdown, no preamble, no trailing commentary."""


def _format_failure_block(entry: dict, label: str) -> str:
    fields = [
        ("ts", entry.get("ts")),
        ("status", entry.get("status")),
        ("reason", entry.get("reason")),
        ("org", entry.get("org_id") or entry.get("org") or entry.get("org_name")),
        ("truck", entry.get("truck") or entry.get("truck_name") or entry.get("truck_title")),
        ("total", entry.get("total") or entry.get("order_total")),
        ("error_message", entry.get("error_message") or entry.get("error")),
        ("confirmation_number", entry.get("confirmation_number")),
        ("final_url", entry.get("final_url")),
        ("source", entry.get("source")),
        ("detail", entry.get("detail")),
    ]
    rendered = "\n".join(f"  {k}: {v}" for k, v in fields if v not in (None, "", []))
    return f"{label}:\n{rendered}" if rendered else f"{label}:\n  (no fields)"


def _build_user_prompt(failure: dict, similar: list[dict]) -> str:
    parts = [_format_failure_block(failure, "CURRENT FAILURE")]
    similar = similar or []
    if similar:
        parts.append(f"\n{len(similar)} similar past failure(s) (most recent first):")
        for i, s in enumerate(similar[:10], start=1):
            parts.append(_format_failure_block(s, f"  PAST #{i}"))
    else:
        parts.append("\nNo similar past failures recorded.")
    return "\n\n".join(parts)


def diagnose_failure(failure: dict, similar: list[dict] | None = None) -> dict:
    """Return an AI-generated diagnosis for one failed autobuy attempt.

    Args:
        failure:  The failure record being viewed. Loose dict — any subset
                  of {ts, status, reason, org_id, truck, total,
                  error_message, final_url, source, detail}.
        similar:  Up to 10 past failure records for pattern-matching.

    Returns:
        ok=True case:
          {"ok": True, "diagnosis", "suggested_action", "model",
           "input_tokens", "output_tokens"}
        ok=False case:
          {"ok": False, "error": "<short reason>", "model": "..."}

    Never raises — a broken AI call must not take down the row render.
    """
    api_key = _read_setting("OPENROUTER_API_KEY")
    if not api_key:
        return {
            "ok": False,
            "error": "OPENROUTER_API_KEY not set. Add it in Settings → API keys.",
            "model": None,
        }
    model = _read_setting("OPENROUTER_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL

    user_prompt = _build_user_prompt(failure, similar or [])

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            # cache_control on the system block is an OpenRouter extension
            # that's passed through to Anthropic. Once the system prompt
            # grows past Anthropic's per-model cacheable minimum, repeat
            # calls within 5 minutes will read it from cache instead of
            # re-billing. Below threshold it's a no-op — keeping it is
            # forward-compatible.
            {"role": "system", "content": [
                {"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "user", "content": user_prompt},
        ],
        # Some OpenRouter providers honour response_format. Harmless when
        # the upstream ignores it; the parse path below is tolerant of
        # plain JSON, fenced JSON, or JSON-with-preamble.
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  _OPENROUTER_REFERER,
        "X-Title":       _OPENROUTER_TITLE,
    }

    try:
        resp = _http().post("/chat/completions", json=payload, headers=headers)
    except httpx.HTTPError as e:
        logger.exception("openrouter request failed")
        return {"ok": False, "error": f"network: {e}", "model": model}

    if resp.status_code != 200:
        body = (resp.text or "")[:400]
        # OpenRouter wraps errors in {"error":{"message":...}}; surface
        # the upstream message to the operator when it's compact.
        try:
            err_obj = resp.json()
            inner = (err_obj.get("error") or {}).get("message") or ""
            if inner:
                body = inner[:400]
        except Exception:
            pass
        logger.warning("openrouter HTTP %s: %s", resp.status_code, body)
        return {
            "ok": False,
            "error": f"openrouter HTTP {resp.status_code}: {body}",
            "model": model,
        }

    try:
        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        raw = message.get("content") or ""
        if isinstance(raw, list):
            # Some providers return content as a list of blocks. Take the
            # first text block.
            raw = next((b.get("text", "") for b in raw if isinstance(b, dict)
                        and b.get("type") == "text"), "")
        usage = data.get("usage") or {}
    except Exception as e:
        logger.exception("openrouter: unexpected response shape")
        return {
            "ok": False,
            "error": f"unexpected response shape: {e}",
            "model": model,
        }

    diagnosis, suggested_action = _parse_json_response(raw)
    if not diagnosis:
        return {
            "ok": False,
            "error": "model returned no parseable diagnosis",
            "raw": (raw or "")[:400],
            "model": model,
        }

    return {
        "ok":               True,
        "diagnosis":        diagnosis,
        "suggested_action": suggested_action or "",
        "model":            model,
        "input_tokens":     int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens":    int(usage.get("completion_tokens", 0) or 0),
        # OpenRouter forwards Anthropic's cache_read_input_tokens when
        # caching engages. Pull it out of the nested details bag when
        # present so the dashboard can surface real cache hits.
        "cache_read_input_tokens": int(
            ((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0
        ),
    }


def _parse_json_response(raw: str) -> tuple[str, str]:
    """Tolerant JSON extraction. The model is instructed to emit raw JSON
    but may wrap it in ```json fences or add prose; reach for the first
    {…} block and parse that."""
    if not raw:
        return "", ""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return "", ""
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return "", ""
    return (
        str(obj.get("diagnosis") or "").strip(),
        str(obj.get("suggested_action") or "").strip(),
    )
