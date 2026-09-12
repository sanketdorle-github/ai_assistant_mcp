"""Unit tests for `core/token_encryption.py`.

Scoped to the encryption helper itself, not `GmailCredentialsRepository` -
this suite's established pattern (see `tests/fakes.py`, `tests/conftest.py`)
is to run fully offline with no real database, and there's no existing
fixture here for a real/fake Mongo collection to test the repository
against. Repository round-tripping is covered by the manual verification
steps instead (see CLAUDE.md's Gmail integration section).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from mcp_orchestration.core.token_encryption import (
    TokenEncryptionError,
    decrypt,
    encrypt,
)


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_encrypt_decrypt_round_trip():
    plaintext = "ya29.some-access-token-value"
    ciphertext = encrypt(plaintext)

    assert ciphertext != plaintext
    assert decrypt(ciphertext) == plaintext


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)

    with pytest.raises(TokenEncryptionError):
        encrypt("some-token")


def test_invalid_key_raises(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-valid-fernet-key")

    with pytest.raises(TokenEncryptionError):
        encrypt("some-token")


def test_decrypt_garbage_raises():
    with pytest.raises(TokenEncryptionError):
        decrypt("not-something-we-ever-encrypted")


def test_decrypt_with_wrong_key_raises(monkeypatch):
    ciphertext = encrypt("some-token")

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

    with pytest.raises(TokenEncryptionError):
        decrypt(ciphertext)
