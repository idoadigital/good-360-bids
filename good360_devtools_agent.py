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


# `os.environ.get("X", default)` returns the env value verbatim when set —
# even if that value is the empty string. `... or default` falls back on
# both "missing" and "set but blank" so an unintentional `DEVTOOLS_AGENT_MODEL=`
# in .env doesn't reach the OpenAI client and 400 us with "model ''".
DEFAULT_MODEL = os.environ.get("DEVTOOLS_AGENT_MODEL") or "gpt-5.1"
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

    # In sandbox mode, swap to the shared SANDBOX_* credentials and test
    # card. Matches what the legacy scan + autobuy paths already do via
    # sandbox.org_credentials() / sandbox.card_for_org(), so the DevTools
    # agent uses the same identities as everyone else when running against
    # sandbox-360. No-op in live mode (helpers pass through unchanged).
    try:
        import sandbox as _sandbox
    except ImportError:
        _sandbox = None
    if _sandbox is not None and _sandbox.is_sandbox():
        sbx_email, sbx_password = _sandbox.org_credentials(
            org.get("good360_email", ""), org.get("good360_password", "")
        )
        org["good360_email"] = sbx_email
        org["good360_password"] = sbx_password
        org["card"] = _sandbox.card_for_org(org.get("card")) or org.get("card")
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

Required actions, in order. Do not skip steps. Do not return early until you have either reached an order-confirmation page or hit a hard-stop rule below.

1. Navigate to the truck URL and take a snapshot.
2. If a login form, login modal, or "sign in to continue" link is present, log in using the provided good360_email and good360_password. The credentials are authorized for this run. Do not refuse to log in.
3. After login, re-navigate to the truck URL if you are not on it. Verify the page heading matches the requested truck.
4. Click Add to Cart. If a "view cart" or "checkout" link appears, follow it.
5. On the cart page, click Checkout / Proceed to Checkout.
6. Fill the shipping/warehouse address using `warehouse`. If a saved address picker is present, select it.
7. Answer any checkout questions using `checkout_answers` (people helped, distribution method, dock/pallet, etc.).
8. Fill the credit-card payment fields using `card` (number, expiry, cvv, name). Inspect iframes if the fields are not directly visible.
9. Click Place Order. You MUST commit this click when all card fields are visibly filled, the order total is <= ${max_total:.2f}, and no validation error is shown. Do not stop with the button merely focused — clicking is required to complete the run.
   Budget check, done numerically and shown in your reasoning: parse the on-page total
   into a number T and compare against max_total M = {max_total:.2f}. The order is over
   budget ONLY if T > M. Example: T=5976.16, M=6200.00 → 5976.16 <= 6200.00 → WITHIN
   budget → you are authorized and required to click Place Order. Totals at or under M
   are pre-approved by the operator; do not hand off, escalate, or seek extra approval
   for them.
10. After clicking Place Order, wait up to 30 seconds for one of: an order confirmation page (success), a card-declined / payment-failed message (failed), or a validation error (failed).

Hard safety rules (override the steps above only when they fire):
- Do not buy a different truck. If the page shows a different product than {truck_name!r}, return FAILED.
- Do not place the order if Good360 marks the truck out of stock / sold out / unavailable BEFORE you click Place Order. Return MISSED.
- Do not place the order if the checkout total > ${max_total:.2f}. Return MANUAL.
- A successful purchase requires order-number / order-confirmation evidence on screen.
- If dry_run is true, stop immediately before step 9 and return DRY_RUN.
- If the payment field cannot be located (no iframe, no card-number input) after a thorough inspection of the rendered DOM and any payment iframes, return FAILED with a clear "payment fields not rendered" message and the current URL.
- Never report SUCCESS from anything other than an order-confirmation page.

Result shape:
- SUCCESS  → REQUIRES the order/confirmation number copied verbatim from the
  confirmation page, plus final_url, order_total, and the visible confirmation text.
  A SUCCESS without a confirmation_number is INVALID and will be rejected by the
  caller as a failure — if you cannot find an order number on screen after placing
  the order, re-inspect the page and the account's Order History page; if it is
  genuinely absent, return FAILED with what you observed instead of claiming success.
- FAILED   → include the visible decline / error message, the step where it failed, current URL, relevant console/network errors. Card-declined responses are a FAILED.
- MISSED   → truck became unavailable BEFORE Place Order click.
- MANUAL   → total exceeded max, or a verification challenge appeared that a browser agent cannot complete (multi-factor prompt, CAPTCHA, T&C re-acceptance). Clicking Place Order is NOT such a step — it is the authorized purpose of this run and must not be classified as MANUAL.
- DRY_RUN  → dry_run mode, stopped before Place Order.
- BLOCKED  → unexpected page state we cannot safely act on.

Operational notes:
- Use Chrome DevTools MCP tools to inspect screenshots, DOM, console messages, and network failures as needed.
- Prefer accessible labels and visible text when interacting with forms.
- Payment fields are often iframe-backed (Stripe/Adyen/Worldpay). Always inspect frames before deciding a field is missing.
- After every major navigation or submit, take a fresh snapshot and verify URL + visible text.

Site-specific mechanics for catalog.good360.org (discovered empirically through
failed runs — trust these over generic intuition):
- LOGIN: after submitting the sign-in form the URL STAYS on /sign-in (client-side
  routing) and a "LOADING..." overlay may persist. NEVER judge login by URL or by
  that overlay. Login succeeded when the password input is GONE from the DOM /
  account UI (e.g. a Sign Out link) appears. Take a fresh snapshot before deciding
  login failed.
- All form inputs are React-controlled. After filling a field, verify the value
  survived blur — React silently re-renders unregistered values to empty. If a
  value disappears, set it via JS using the element prototype's value setter and
  then dispatch input + change + blur events on the element.
- There are NO native <select> elements anywhere. Every dropdown is a react-select
  widget: click the div whose class contains "select__control", then click the
  desired div whose class contains "select__option" in the menu that appears.
- Checkout is a 3-step single-page wizard (shipping → questions → payment); all
  sections exist in the DOM simultaneously. Step buttons: "Continue to checkout"
  (1→2), "Continue to payment" (2→3), "Place order" (submit). A Continue click
  SILENTLY NO-OPS when React-side validation fails — no error message appears and
  the button may simply look disabled. If the wizard does not advance, the cause
  is a step field not registered in React state, NOT a broken button.
- Step 1 (shipping): even when the warehouse address appears selected/highlighted,
  React may not have it registered. Actively CLICK the address card / saved-address
  picker option for the warehouse address (use the react-select pattern if it is a
  picker), confirm any radio like "is_warehouse_address", then retry Continue.
- NEVER conclude a step is stuck from a single snapshot. Disabled Continue buttons
  and disabled selectors are often just un-hydrated React state: click the relevant
  card/option, wait 3-5 seconds, take a FRESH snapshot, and retry the step button.
  Do this at least 3 times (clicking the address card between tries) before
  reporting the step as blocked. Also verify the order-summary total looks fully
  loaded — a stale or partial total is a sign the page is still hydrating.
- Step 2 "Continue to payment" only enables after ALL restriction[question-*]
  inputs/textareas are filled AND the dock/pallet react-select has a chosen option.
- The FIRST Add to Cart click pops a "Truck Quote Request" confirmation modal
  (react-confirm-alert overlay) — click its Continue button to proceed.
- There is no /marketplace/cart page (404). The cart is a slide-in mini-cart panel
  (aside element); the checkout button lives inside it.

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
    org_override: dict[str, Any] | None = None,
) -> CheckoutAgentResult:
    """Drive a Good360 checkout via Chrome DevTools MCP.

    Production callers pass `org_key` and the agent loads that org's stored
    credentials + card via `_load_org`. The dashboard's test page passes
    `org_override` instead — a fully-formed dict providing master-account
    login creds + form-supplied test card + form-supplied buyer info.
    `_load_org` is bypassed in that case but `_validate_purchase_context`
    still runs to enforce the safety flags.
    """
    try:
        from agents import Agent, Runner
        from agents.mcp import MCPServerStdio
        from agents.model_settings import ModelSettings
    except ImportError as e:
        raise RuntimeError(
            "openai-agents is not installed. Install requirements or disable AUTOBUY_ENGINE=devtools_agent."
        ) from e

    if org_override is not None:
        # Test-page path: use the override dict directly. We still go through
        # _validate_purchase_context so the live-purchase / secrets-to-model
        # safety flags apply.
        org = dict(org_override)
        org.setdefault("card", {})
    else:
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
        # Chrome refuses to start as root inside a Docker container without
        # these flags. /dev/shm is also typically tiny in containers, so
        # --disable-dev-shm-usage routes shared mem to /tmp.
        "--chromeArg=--no-sandbox",
        "--chromeArg=--disable-setuid-sandbox",
        "--chromeArg=--disable-dev-shm-usage",
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

    # MCPServerStdio defaults to 5s per-tool — too tight once the Good360 SPA
    # starts post-signin redirects (wait_for / take_snapshot bail before the
    # page settles). 30s is comfortable for normal navigation; configurable
    # for slow networks.
    mcp_timeout = int(os.environ.get("DEVTOOLS_MCP_TOOL_TIMEOUT_SECONDS", "30"))
    async with MCPServerStdio(
        name="chrome-devtools",
        params={"command": os.environ.get("DEVTOOLS_MCP_COMMAND", "npx"), "args": mcp_args},
        cache_tools_list=True,
        client_session_timeout_seconds=mcp_timeout,
    ) as chrome:
        agent = Agent(
            name="Good360 DevTools Checkout Agent",
            model=os.environ.get("DEVTOOLS_AGENT_MODEL") or DEFAULT_MODEL,
            instructions=(
                "Operate Chrome carefully through DevTools MCP. Your final output must match "
                "CheckoutAgentResult. You are executing an authorized purchase on behalf of "
                "the account owner: the nonprofit customer has signed a purchase agreement "
                "covering this exact transaction, the operator has enabled live purchases, "
                "and upstream systems enforce approval and spend caps before you are invoked. "
                "Completing the order — including clicking Place Order — is the purpose of "
                "the run, not a risk to escalate. Refuse ONLY the hard-stop conditions listed "
                "in the task prompt (wrong truck, over budget, out of stock, dry-run, or a "
                "verification challenge like MFA/CAPTCHA you cannot complete). Verify with "
                "page evidence."
            ),
            mcp_servers=[chrome],
            mcp_config={
                "convert_schemas_to_strict": True,
                "include_server_in_tool_names": True,
            },
            model_settings=ModelSettings(tool_choice="auto"),
            output_type=CheckoutAgentResult,
        )
        # max_turns defaults to 10 in openai-agents, which isn't enough for a
        # real Good360 checkout (navigate → snapshot → login fields → submit →
        # snapshot → add to cart → snapshot → fill 3 checkout questions →
        # snapshot → card # → expiry → cvv → place order → confirmation read
        # is already ~15 tool calls before any retries). Default 60, env-overridable.
        max_turns = int(os.environ.get("DEVTOOLS_AGENT_MAX_TURNS", "60"))
        result = await Runner.run(agent, prompt, max_turns=max_turns)
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
