"""Sandbox-mode routing.

When SANDBOX_MODE=true, every URL/cred/card the scan + autobuy stack uses
swaps to a test site (default https://sandbox-360.netlify.app). Lets us
exercise the full pipeline end-to-end against a fake Good360 without ever
hitting the real marketplace.

Single source of truth so we never end up half-sandboxed: every script that
talks to Good360 should call into here for its URLs and credentials.
"""
from __future__ import annotations

import os
from urllib.parse import urljoin

LIVE_BASE_URL = "https://catalog.good360.org"
LIVE_LOGIN_URL = "https://catalog.good360.org/marketplace/home"
LIVE_BROWSE_URL = "https://catalog.good360.org/marketplace/browse-goods/truckload-donations/amazon.html"
LIVE_CART_URL = "https://marketplace.good360.org/cart"
LIVE_CHECKOUT_URL = "https://catalog.good360.org/marketplace/checkout/cart"
LIVE_AUTOBUY_LOGIN_URL = "https://marketplace.good360.org/login"

DEFAULT_SANDBOX_BASE_URL = "https://sandbox-360.netlify.app"


def is_sandbox() -> bool:
    return (os.environ.get("SANDBOX_MODE", "") or "").strip().lower() in ("1", "true", "yes", "on")


def sandbox_base_url() -> str:
    return (os.environ.get("SANDBOX_GOOD360_BASE_URL") or DEFAULT_SANDBOX_BASE_URL).rstrip("/")


def _swap_host(live_url: str) -> str:
    """Return live_url with its host swapped to the sandbox base.

    Path + query are preserved — the sandbox is meant to mirror the live URL
    structure. If the sandbox ever diverges, add explicit overrides here
    rather than scattering host checks across the codebase.
    """
    if not is_sandbox():
        return live_url
    base = sandbox_base_url()
    # Strip protocol+host from the live URL and graft what's left onto the
    # sandbox base. Robust against both /path and full https://host/path
    # inputs.
    from urllib.parse import urlparse
    parsed = urlparse(live_url)
    suffix = parsed.path or "/"
    if parsed.query:
        suffix += "?" + parsed.query
    return urljoin(base + "/", suffix.lstrip("/"))


def good360_login_url() -> str:
    return _swap_host(LIVE_LOGIN_URL)


def good360_browse_url() -> str:
    return _swap_host(LIVE_BROWSE_URL)


def good360_cart_url() -> str:
    return _swap_host(LIVE_CART_URL)


def good360_checkout_url() -> str:
    return _swap_host(LIVE_CHECKOUT_URL)


def good360_autobuy_login_url() -> str:
    return _swap_host(LIVE_AUTOBUY_LOGIN_URL)


def good360_base_url() -> str:
    """Host portion only, used to absolutize relative hrefs scraped from listings."""
    return sandbox_base_url() if is_sandbox() else LIVE_BASE_URL


# ----- credentials -----

def scan_credentials(live_email: str = "", live_password: str = "") -> tuple[str, str]:
    """Return (email, password) the scanner should use.

    In sandbox mode, falls back to SCAN_GOOD360_* live values only if the
    sandbox creds are unset, so a half-configured sandbox doesn't accidentally
    log into the live site.
    """
    if is_sandbox():
        return (
            os.environ.get("SANDBOX_GOOD360_EMAIL", "") or live_email,
            os.environ.get("SANDBOX_GOOD360_PASSWORD", "") or live_password,
        )
    return live_email, live_password


def org_credentials(org_email: str, org_password: str) -> tuple[str, str]:
    """In sandbox mode, every org logs in with the single shared sandbox account."""
    if is_sandbox():
        return (
            os.environ.get("SANDBOX_GOOD360_EMAIL", "") or org_email,
            os.environ.get("SANDBOX_GOOD360_PASSWORD", "") or org_password,
        )
    return org_email, org_password


# ----- payment cards -----

def card_for_org(live_card: dict | None) -> dict | None:
    """Return the card dict to charge.

    `live_card` is the per-org card normally pulled from settings/QuickBeed.
    In sandbox mode we replace it wholesale with the SANDBOX_CARD_* values so
    no real PAN ever reaches the test browser.
    """
    if is_sandbox():
        return {
            "name":   os.environ.get("SANDBOX_CARD_NAME", "") or "Sandbox Tester",
            "number": os.environ.get("SANDBOX_CARD_NUMBER", "") or "4242424242424242",
            "expiry": os.environ.get("SANDBOX_CARD_EXPIRY", "") or "1230",
            "cvv":    os.environ.get("SANDBOX_CARD_CVV", "") or "123",
            "type":   os.environ.get("SANDBOX_CARD_TYPE", "") or "visa",
        }
    return live_card


def env_card_fields(live_prefix: str) -> tuple[str, str, str, str, str]:
    """Convenience for the legacy autobuy that reads CARD_<ORG>_* env vars directly.

    Returns (number, expiry_MMYY, cvv, name, type). In sandbox mode the
    SANDBOX_CARD_* values are returned regardless of which org's prefix the
    caller passed.
    """
    if is_sandbox():
        c = card_for_org(None) or {}
        return c["number"], c["expiry"], c["cvv"], c["name"], c["type"]
    return (
        os.environ.get(f"{live_prefix}_NUMBER", ""),
        os.environ.get(f"{live_prefix}_EXPIRY", ""),
        os.environ.get(f"{live_prefix}_CVV", ""),
        os.environ.get(f"{live_prefix}_NAME", ""),
        os.environ.get(f"{live_prefix}_TYPE", ""),
    )


# ----- alert decoration -----

def alert_prefix() -> str:
    """Prefix every Telegram/email alert with [SANDBOX] when sandbox is on.

    Includes a trailing space so callers can do `alert_prefix() + "..."` and
    get a clean message either way.
    """
    return "[SANDBOX] " if is_sandbox() else ""


def decorate_alert(message: str) -> str:
    """Idempotently prepend [SANDBOX] to a message when sandbox is on."""
    if not is_sandbox() or not message:
        return message
    if message.lstrip().startswith("[SANDBOX]"):
        return message
    return alert_prefix() + message
