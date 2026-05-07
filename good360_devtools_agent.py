#!/usr/bin/env python3
"""Good360 checkout agent powered by Chrome DevTools MCP.

This is an optional checkout engine. It is intentionally isolated from the
legacy Playwright scripts so the monitor can opt into it with
AUTOBUY_ENGINE=devtools_agent without removing the existing fallback path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

import config as _cfg


DEFAULT_MODEL = os.environ.get("DEVTOOLS_AGENT_MODEL", "gpt-5.1")
DEFAULT_MAX_TOTAL = float(os.environ.get("MAX_AUTO_PAY", "6400"))


try:
    from pydantic import BaseModel, Field

    class CheckoutAgentResult(BaseModel):
        status: str = Field(
            description="One of SUCCESS, MISSED, FAILED, MANUAL, DRY_RUN, or BLOCKED."
        )
        message: str
        order_total: float | None = None
        confirmation_number: str | None = None
        evidence: list[str] = Field(default_factory=list)
        final_url: str | None = None

except ImportError:
    @dataclass
    class CheckoutAgentResult:
        status: str
        message: str
        order_total: float | None = None
        confirmation_number: str | None = None
        evidence: list[str] = dataclass_field(default_factory=list)
        final_url: str | None = None

        @classmethod
        def model_validate(cls, data: dict[str, Any]) -> "CheckoutAgentResult":
            allowed = {k: data.get(k) for k in cls.__dataclass_fields__}
            return cls(**allowed)

        def model_dump_json(self, indent: int | None = None) -> str:
            return json.dumps(asdict(self), indent=indent)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _redact(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _org_card_from_env(org_key: str) -> dict[str, str]:
    prefix = "CARD_" + org_key.upper().replace("-", "_")
    return {
        "name": os.environ.get(f"{prefix}_NAME", ""),
        "number": os.environ.get(f"{prefix}_NUMBER", ""),
        "expiry": os.environ.get(f"{prefix}_EXPIRY", ""),
        "cvv": os.environ.get(f"{prefix}_CVV", ""),
        "type": os.environ.get(f"{prefix}_TYPE", "visa"),
    }


def _load_org(org_key: str) -> dict[str, Any]:
    orgs = _cfg.load_orgs()
    org = orgs.get(org_key)
    if not org:
        raise RuntimeError(f"Org {org_key!r} not configured or missing required Good360 secrets")

    org = dict(org)
    org.setdefault("card", _org_card_from_env(org_key))
    return org


def _validate_purchase_context(org_key: str, org: dict[str, Any], dry_run: bool) -> None:
    required = {
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "good360_email": org.get("good360_email"),
        "good360_password": org.get("good360_password"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError("Missing required agent checkout values: " + ", ".join(missing))

    if not dry_run and not _truthy(os.environ.get("DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE")):
        raise RuntimeError(
            "DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE is not true; refusing live Place Order."
        )

    if not dry_run and not _truthy(os.environ.get("DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL")):
        raise RuntimeError(
            "DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL is not true; refusing to expose checkout secrets to the agent."
        )

    card = org.get("card") or {}
    if not dry_run:
        card_missing = [k for k in ("number", "expiry", "cvv") if not card.get(k)]
        if card_missing:
            raise RuntimeError(f"Org {org_key!r} card is missing: {', '.join(card_missing)}")


def _build_prompt(
    *,
    org_key: str,
    org: dict[str, Any],
    truck_name: str,
    truck_url: str,
    max_total: float,
    dry_run: bool,
) -> str:
    card = org.get("card") or {}
    answers = org.get("checkout_answers") or {}
    safe_card = {
        **card,
        "number": _redact(card.get("number")),
        "cvv": "***" if card.get("cvv") else "",
    }

    sensitive_payload = {
        "good360_email": org.get("good360_email"),
        "good360_password": org.get("good360_password"),
        "card": card if not dry_run else safe_card,
    }
    public_payload = {
        "org_key": org_key,
        "org_name": org.get("name", org_key),
        "truck_name": truck_name,
        "truck_url": truck_url,
        "max_total": max_total,
        "dry_run": dry_run,
        "checkout_answers": answers,
        "warehouse": org.get("warehouse") or org.get("warehouse_address") or "",
        "billing": org.get("billing") or org.get("billing_address") or "",
        "card": card if not dry_run else safe_card,
    }

    return f"""
You are the Good360 checkout operator for an automated truckload purchase.

Goal:
- Buy exactly this truck if it is available and the total is <= ${max_total:.2f}.
- Truck name: {truck_name}
- Truck URL: {truck_url}
- Organization: {org.get("name", org_key)}

Hard safety rules:
- Do not buy a different truck.
- Do not continue if the page says sold out, unavailable, no longer available, or out of stock.
- Do not place the order if the checkout total is greater than ${max_total:.2f}.
- Do not place the order if you cannot see a final review/submit page for this exact truck.
- A successful purchase requires real confirmation evidence: order number, order confirmation, order receipt, or equivalent.
- If dry_run is true, stop immediately before clicking Place Order and return DRY_RUN with evidence.
- If an unexpected page error, validation error, modal, iframe, or blocked control appears, inspect console/network/DOM, fix the interaction if it is safe, and continue only when the page state is understood.
- Never report SUCCESS from a product page, cart page, or payment page. SUCCESS only means an order confirmation page exists.
- If payment fails or is declined, return FAILED and include the visible decline message, validation errors, current URL, relevant console errors, relevant network failures, and the checkout step where it failed in evidence.
- If payment succeeds, include confirmation number, final URL, order total, and all visible success indicators in evidence.

Operational notes:
- Use Chrome DevTools MCP tools to inspect screenshots, DOM, console messages, and network failures as needed.
- Prefer accessible labels and visible text when interacting with forms.
- Payment fields may be iframe-backed; inspect frames before deciding a field is missing.
- After every major navigation or submit, verify the current URL and visible page text.

Checkout data:
{json.dumps(public_payload, indent=2)}

Sensitive data authorized for this run:
{json.dumps(sensitive_payload, indent=2)}

Return only a structured CheckoutAgentResult.
"""


async def run_agent(
    *,
    org_key: str,
    truck_name: str,
    truck_url: str,
    admin_fee: float,
    dry_run: bool,
) -> CheckoutAgentResult:
    try:
        from agents import Agent, Runner
        from agents.mcp import MCPServerStdio
        from agents.model_settings import ModelSettings
    except ImportError as e:
        raise RuntimeError(
            "openai-agents is not installed. Install requirements or disable AUTOBUY_ENGINE=devtools_agent."
        ) from e

    org = _load_org(org_key)
    _validate_purchase_context(org_key, org, dry_run=dry_run)

    max_total = float(org.get("max_auto_pay") or org.get("max_price") or DEFAULT_MAX_TOTAL)
    if admin_fee and admin_fee > max_total:
        return CheckoutAgentResult(
            status="MANUAL",
            message=f"Detected admin fee ${admin_fee:.2f} exceeds max ${max_total:.2f}",
            order_total=admin_fee,
        )

    mcp_args = [
        "-y",
        "chrome-devtools-mcp@latest",
        "--headless",
        "--no-usage-statistics",
        "--redact-network-headers",
    ]
    chrome_executable = os.environ.get("DEVTOOLS_CHROME_EXECUTABLE")
    if chrome_executable:
        mcp_args.append(f"--executable-path={chrome_executable}")
    if _truthy(os.environ.get("DEVTOOLS_AGENT_ISOLATED", "true")):
        mcp_args.append("--isolated")

    prompt = _build_prompt(
        org_key=org_key,
        org=org,
        truck_name=truck_name,
        truck_url=truck_url,
        max_total=max_total,
        dry_run=dry_run,
    )

    async with MCPServerStdio(
        name="chrome-devtools",
        params={"command": os.environ.get("DEVTOOLS_MCP_COMMAND", "npx"), "args": mcp_args},
        cache_tools_list=True,
    ) as chrome:
        agent = Agent(
            name="Good360 DevTools Checkout Agent",
            model=os.environ.get("DEVTOOLS_AGENT_MODEL", DEFAULT_MODEL),
            instructions=(
                "Operate Chrome carefully through DevTools MCP. Your final output must match "
                "CheckoutAgentResult. Refuse unsafe purchases. Verify with page evidence."
            ),
            mcp_servers=[chrome],
            mcp_config={
                "convert_schemas_to_strict": True,
                "include_server_in_tool_names": True,
            },
            model_settings=ModelSettings(tool_choice="auto"),
            output_type=CheckoutAgentResult,
        )
        result = await Runner.run(agent, prompt)
        if isinstance(result.final_output, CheckoutAgentResult):
            return result.final_output
        if isinstance(result.final_output, dict):
            return CheckoutAgentResult.model_validate(result.final_output)
        return CheckoutAgentResult(status="FAILED", message=str(result.final_output))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Good360 checkout through Chrome DevTools MCP agent")
    parser.add_argument("truck_name")
    parser.add_argument("truck_url")
    parser.add_argument("admin_fee", type=float, nargs="?", default=0.0)
    parser.add_argument("org_key")
    parser.add_argument("--dry-run", action="store_true", default=_truthy(os.environ.get("DEVTOOLS_AGENT_DRY_RUN")))
    args = parser.parse_args()

    try:
        result = asyncio.run(
            run_agent(
                org_key=args.org_key,
                truck_name=args.truck_name,
                truck_url=args.truck_url,
                admin_fee=args.admin_fee,
                dry_run=args.dry_run,
            )
        )
    except Exception as e:
        result = CheckoutAgentResult(status="FAILED", message=str(e))

    print(result.model_dump_json(indent=2))
    return {
        "SUCCESS": 0,
        "FAILED": 1,
        "MISSED": 2,
        "MANUAL": 3,
        "DRY_RUN": 6,
        "BLOCKED": 7,
    }.get(result.status, 1)


if __name__ == "__main__":
    sys.exit(main())
