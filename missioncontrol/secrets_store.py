"""Encryption-at-rest for sensitive settings (card data, tokens, passwords).

Uses AES-GCM with a 256-bit master key supplied via DASHBOARD_MASTER_KEY env var
(base64-url, 32 raw bytes). Keep the key out of the database — that's the whole
point. If the key leaks, every encrypted value is compromised.

Storage format (bytes): nonce(12) || ciphertext+tag

This is *not* a substitute for a real KMS / Vault / 1Password Connect — the
master key still lives next to the database in .env. But it does mean a
DB-only exfiltration (e.g. backup theft) doesn't yield plaintext cards.
"""
from __future__ import annotations

import base64
import os
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _load_master_key() -> bytes:
    raw = os.environ.get("DASHBOARD_MASTER_KEY", "").strip()
    if not raw:
        raise RuntimeError(
            "DASHBOARD_MASTER_KEY not set. Generate one with: "
            "python -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'"
        )
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise RuntimeError(f"DASHBOARD_MASTER_KEY is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise RuntimeError(f"DASHBOARD_MASTER_KEY must decode to 32 bytes, got {len(key)}")
    return key


_KEY = None


def _key() -> bytes:
    global _KEY
    if _KEY is None:
        _KEY = _load_master_key()
    return _KEY


def encrypt(plaintext: str) -> bytes:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ct


def decrypt(blob: bytes) -> str:
    if len(blob) < 13:
        raise ValueError("ciphertext too short")
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(_key()).decrypt(nonce, ct, None).decode("utf-8")


def generate_master_key_b64() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
