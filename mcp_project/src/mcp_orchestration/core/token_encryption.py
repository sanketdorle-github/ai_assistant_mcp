"""Symmetric encryption for secrets we persist ourselves (currently: Gmail
OAuth access/refresh tokens in the `gmail_credentials` collection).

Uses `cryptography.fernet.Fernet` with a single app-level key from the
`TOKEN_ENCRYPTION_KEY` env var. This is deliberately simple compared to
envelope encryption via a cloud KMS: one key to manage and rotate, but a
large step up from the plaintext-on-disk token file this replaces, and
appropriate for the scale of this project. Swap for a KMS-backed scheme if
you outgrow it.

The key is only required at call time (`encrypt`/`decrypt`), not at import
time, so the app still boots without it - Gmail is an optional feature the
same way missing OAuth client credentials already disable it in
`mcp/config.py`.
"""

import os

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionError(RuntimeError):
    """Raised when TOKEN_ENCRYPTION_KEY is missing, or a value can't be
    decrypted with it (wrong/rotated key, or corrupted data)."""


def _fernet() -> Fernet:
    key = os.getenv("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())" and set it in your environment.'
        )
    try:
        return Fernet(key.encode("utf-8"))
    except ValueError as e:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key."
        ) from e


def encrypt(value: str) -> str:
    """Encrypt a plaintext string, returning an opaque token-safe string."""
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str) -> str:
    """Decrypt a string produced by `encrypt`. Never logs the value either
    direction - callers should do the same."""
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise TokenEncryptionError(
            "Could not decrypt stored value - wrong TOKEN_ENCRYPTION_KEY or corrupted data."
        ) from e
