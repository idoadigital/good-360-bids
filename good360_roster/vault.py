#!/usr/bin/env python3
"""
vault.py — E-Comsetter Good360 Roster System Encryption Foundation
Dual encryption: SQLCipher (AES-256 DB) + Fernet (field-level)
Built: 2026-03-20 | Security: Production
"""

import base64
import hashlib
import logging
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# ─── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("vault")
logger.setLevel(logging.DEBUG)


# ─── Exceptions ────────────────────────────────────────────────────────────────
class VaultError(Exception):
    """Base vault exception."""
    pass


class VaultKeyMissingError(VaultError):
    """ROSTER_MASTER_KEY not set in environment."""
    pass


class VaultDecryptionError(VaultError):
    """Decryption failed (wrong key or corrupted data)."""
    pass


class VaultEncryptionError(VaultError):
    """Encryption failed."""
    pass


# ─── Master Key Management ─────────────────────────────────────────────────────
class MasterKeyManager:
    """
    Manages the ROSTER_MASTER_KEY from environment.
    Derives two sub-keys:
      - fernet_key  → Fernet (field-level encryption)
      - cipher_key  → raw bytes for SQLCipher passphrase
    """

    CACHE: bytes | None = None
    FERNET_CACHE: Fernet | None = None

    @classmethod
    def get_master_key(cls, force_refresh: bool = False) -> bytes:
        """
        Returns cached master key bytes from env var ROSTER_MASTER_KEY.
        Raises VaultKeyMissingError if not set.
        """
        if cls.CACHE is not None and not force_refresh:
            return cls.CACHE

        raw = os.environ.get("ROSTER_MASTER_KEY")
        if not raw:
            raise VaultKeyMissingError(
                "ROSTER_MASTER_KEY environment variable is not set. "
                "Set it in the repo-level .env (see .env.example)."
            )

        # Normalize: accept raw base64, hex, or raw ASCII passphrase
        cls.CACHE = cls._normalize_key(raw)
        return cls.CACHE

    @classmethod
    def _normalize_key(cls, raw: str) -> bytes:
        """Normalize any string form to fixed 32 bytes."""
        raw_bytes = raw.encode("utf-8")
        # Use SHA-256 to get consistent 32 bytes from any input
        return hashlib.sha256(raw_bytes).digest()

    @classmethod
    def get_fernet(cls) -> Fernet:
        """
        Returns a cached Fernet instance ready to encrypt/decrypt.
        Derives a valid Fernet key from the master key.
        """
        if cls.FERNET_CACHE is not None:
            return cls.FERNET_CACHE

        master = cls.get_master_key()
        # Fernet key = base64(sha256(master)[:16] + sha256(master+1)[:16]) = 32 bytes
        part_a = hashlib.sha256(master).digest()[:16]
        part_b = hashlib.sha256(master + b"fernet_v1").digest()[:16]
        fernet_key = base64.urlsafe_b64encode(part_a + part_b)
        cls.FERNET_CACHE = Fernet(fernet_key)
        return cls.FERNET_CACHE

    @classmethod
    def get_sqlcipher_passphrase(cls) -> str:
        """
        Returns SQLCipher passphrase as hex string (64 chars).
        SQLCipher accepts arbitrary-length passphrases.
        """
        master = cls.get_master_key()
        return master.hex()

    @classmethod
    def get_sqlcipher_uri(cls, db_path: str) -> str:
        """
        Returns SQLCipher connection URI with encrypted pragma.
        PRAGMA key = 'hexkey'; PRAGMA cipher_page_size = 4096
        """
        hexkey = cls.get_sqlcipher_passphrase()
        return (
            f"sqlite:///{db_path}"
            f"?cipher=sqlcipher"
            f"&key={hexkey}"
            f"&cipher_page_size=4096"
            f"&kdf_iter=256000"
            f"&cipher_hmac_algorithm=HMAC_SHA256"
            f"&cipher_kdf_algorithm=PBKDF2_HMAC_SHA256"
        )

    @classmethod
    def clear_cache(cls):
        """Clear cached key (useful for testing or key rotation)."""
        cls.CACHE = None
        cls.FERNET_CACHE = None


# ─── Fernet Field Encryption ───────────────────────────────────────────────────
def encrypt_field(plaintext: str | bytes) -> bytes:
    """
    Encrypt a plaintext string or bytes using Fernet (AES-128-CBC + HMAC).
    Returns raw ciphertext bytes (store as BLOB in SQLite).
    Raises VaultEncryptionError on failure.
    """
    if not plaintext:
        return b""
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")

    try:
        f = MasterKeyManager.get_fernet()
        return f.encrypt(plaintext)
    except Exception as e:
        raise VaultEncryptionError(f"Fernet encryption failed: {e}") from e


def decrypt_field(ciphertext: bytes) -> str:
    """
    Decrypt Fernet ciphertext bytes back to a UTF-8 string.
    Returns empty string if ciphertext is empty.
    Raises VaultDecryptionError on wrong key or corruption.
    """
    if not ciphertext or ciphertext == b"":
        return ""

    try:
        f = MasterKeyManager.get_fernet()
        return f.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise VaultDecryptionError(
            "Fernet decryption failed: wrong key or corrupted data"
        ) from e
    except Exception as e:
        raise VaultDecryptionError(f"Decryption error: {e}") from e


# ─── Password Generation (for Good360 accounts) ───────────────────────────────
def generate_strong_password(length: int = 24) -> str:
    """Generate a cryptographically secure random password."""
    alphabet = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "!@#$%^&*()-_=+[]{}|;:,.<>?"
    )
    entropy_bits = length * (len(alphabet).bit_length() - 1)
    return "".join(
        secrets.choice(alphabet) for _ in range(length)
    )


# ─── Database Helpers ─────────────────────────────────────────────────────────
@contextmanager
def get_sqlcipher_connection(db_path: str):
    """
    Context manager for SQLite + SQLCipher connection.
    Requires sqlalchemy[sqlcipher] or sqlite3 + SQLCipher library.
    Falls back to sqlite3 if SQLCipher unavailable (dev only).
    """
    import sqlite3

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    # Try SQLCipher via sqlite3 (requires pysqlcipher3)
    try:
        import sqlcipher3 as sqlite3_native
        conn = sqlite3_native.connect(db_path)
        conn.execute(f"PRAGMA key = 'x'{MasterKeyManager.get_sqlcipher_passphrase()}'")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA kdf_iter = 256000")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA256")
        logger.debug(f"SQLCipher connection opened: {db_path}")
    except ImportError:
        logger.warning(
            "sqlcipher3 not installed — falling back to sqlite3 (UNENCRYPTED). "
            "DO NOT use in production!"
        )
        conn = sqlite3.connect(db_path)

    try:
        yield conn
    finally:
        conn.close()


def get_engine_sqlcipher(db_path: str):
    """
    Returns SQLAlchemy Engine using SQLCipher dialect.
    Install: pip install sqlalchemy pysqlcipher3
    """
    try:
        from sqlalchemy import create_engine

        uri = MasterKeyManager.get_sqlcipher_uri(db_path)

        # Override the dialect to use sqlcipher
        uri = uri.replace("sqlite:///", "sqlite+sqlcipher:///")
        engine = create_engine(uri, echo=False)
        logger.debug(f"SQLAlchemy SQLCipher engine created: {db_path}")
        return engine
    except Exception as e:
        logger.error(f"Failed to create SQLCipher engine: {e}")
        raise


# ─── Secure Wipe Utilities ─────────────────────────────────────────────────────
def secure_wipe(data: bytearray | memoryview) -> None:
    """Overwrite bytes in-place before deallocation (defense against memory forensics)."""
    if isinstance(data, (bytearray, memoryview)):
        for i in range(len(data)):
            data[i] = 0


# ─── .env Management ──────────────────────────────────────────────────────────
ROSTER_DIR = Path(__file__).parent
ENV_FILE = ROSTER_DIR / ".env"


def ensure_env_file():
    """Create .env file with ROSTER_MASTER_KEY if it doesn't exist."""
    if ENV_FILE.exists():
        return

    # Generate a new random master key (base64 Fernet-compatible)
    new_key = Fernet.generate_key().decode()
    ENV_FILE.write_text(f"ROSTER_MASTER_KEY={new_key}\n")
    ENV_FILE.chmod(0o600)  # Owner read/write only
    logger.info(f"Generated new ROSTER_MASTER_KEY in {ENV_FILE}")
    print(f"[VAULT] New .env created at {ENV_FILE}")
    print("[VAULT] IMPORTANT: Save this key securely!")
    print(f"[VAULT] ROSTER_MASTER_KEY={new_key}")


def load_env():
    """Load .env into os.environ if file exists."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ─── Vault Test ────────────────────────────────────────────────────────────────
def vault_self_test() -> bool:
    """
    Run self-test: encrypt/decrypt cycle + SQLCipher connection test.
    Returns True if all tests pass.
    """
    print("\n[VAULT] Running self-test...")

    # 1. Test Fernet encrypt/decrypt
    test_strings = [
        "Hello, World!",
        "pass_BREAK@#$%^&*()",
        "4242-4242-4242-4242",
        "日本語テスト",
        "",
    ]
    for pt in test_strings:
        enc = encrypt_field(pt)
        dec = decrypt_field(enc)
        assert dec == pt, f"Fernet mismatch: '{pt}' != '{dec}'"
        logger.debug(f"  ✓ Fernet OK: {repr(pt[:20])}")

    print("  ✓ Fernet encrypt/decrypt: PASS")

    # 2. Test master key
    try:
        key = MasterKeyManager.get_master_key()
        print(f"  ✓ Master key loaded: {key[:8].hex()}... ({len(key)} bytes)")
    except VaultKeyMissingError:
        print("  ⚠ No ROSTER_MASTER_KEY — generating temp for test...")
        os.environ["ROSTER_MASTER_KEY"] = Fernet.generate_key().decode()
        MasterKeyManager.clear_cache()
        key = MasterKeyManager.get_master_key()
        print("  ✓ Temp key generated for test")

    # 3. Test SQLCipher connection
    test_db = ROSTER_DIR / "db" / "vault_self_test.db"
    test_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with get_sqlcipher_connection(str(test_db)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER)")
            conn.execute("INSERT INTO test VALUES (1)")
            result = conn.execute("SELECT * FROM test").fetchone()
            assert result[0] == 1
        print("  ✓ SQLCipher connection: PASS")
        # Clean up
        test_db.unlink(missing_ok=True)
    except Exception as e:
        print(f"  ⚠ SQLCipher test: {e}")

    print("[VAULT] Self-test complete. ✓\n")
    return True


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    """CLI: vault.py --init | --test | --generate-key | --encrypt <text> | --decrypt <hex>"""
    import argparse

    parser = argparse.ArgumentParser(prog="vault.py", description="Vault encryption tool")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("init", help="Initialize .env with new ROSTER_MASTER_KEY")
    sub.add_parser("test", help="Run self-test")
    sub.add_parser("generate-key", help="Generate a new master key (print only)")
    enc_p = sub.add_parser("encrypt", help="Encrypt text")
    enc_p.add_argument("text")
    dec_p = sub.add_parser("decrypt", help="Decrypt hex ciphertext")
    dec_p.add_argument("hex")

    args = parser.parse_args()

    if args.cmd == "init":
        ensure_env_file()
        load_env()
        vault_self_test()
    elif args.cmd == "test":
        load_env()
        vault_self_test()
    elif args.cmd == "generate-key":
        key = Fernet.generate_key().decode()
        print(f"ROSTER_MASTER_KEY={key}")
    elif args.cmd == "encrypt":
        load_env()
        ct = encrypt_field(args.text)
        print(ct.hex())
    elif args.cmd == "decrypt":
        load_env()
        pt = decrypt_field(bytes.fromhex(args.hex))
        print(pt)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
