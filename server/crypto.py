"""Symmetric encryption for token/secret columns (token_enc / value_enc).

A single server-side key `SECRETS_KEY` (Fernet, base64 urlsafe 32-byte) encrypts every
Gitea token and per-project secret before it touches Postgres. Losing the key means the
stored ciphertext is unrecoverable — it is set once in the service .env on 242 and never
committed. Keeping tokens plaintext in the DB would be a phone-grade leak (house rule).
"""
import os
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_SECRETS_KEY = os.environ.get("SECRETS_KEY", "").strip()

# Fail loud at import if unset in a real deploy — but allow module import for tooling
# (a missing key only errors when encrypt/decrypt is actually called).
_fernet: Fernet | None = None
if _SECRETS_KEY:
    try:
        _fernet = Fernet(_SECRETS_KEY.encode())
    except Exception as e:  # noqa: BLE001
        logger.error("SECRETS_KEY is not a valid Fernet key: %s", e)
        _fernet = None


def _require() -> Fernet:
    if _fernet is None:
        raise RuntimeError(
            "SECRETS_KEY is unset or invalid — cannot encrypt/decrypt secrets. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        )
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 secret -> urlsafe token string for a *_enc column."""
    return _require().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a *_enc column back to the plaintext secret."""
    try:
        return _require().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError("failed to decrypt secret (wrong SECRETS_KEY?)") from e
