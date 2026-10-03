"""Symmetric encryption for secrets stored in SQLite (Fernet, key from ENCRYPTION_KEY)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    """A stored secret could not be decrypted (wrong ENCRYPTION_KEY or corrupt value)."""


class SecretBox:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretError("Stored secret could not be decrypted; was ENCRYPTION_KEY changed?") from exc
