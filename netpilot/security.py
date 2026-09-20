"""Secret handling for netpilot.

Credentials must live somewhere at rest, and storing router passwords in plaintext in a
SQLite file is not acceptable — even for a lab tool. Every secret is encrypted with
Fernet (AES-128-CBC + HMAC-SHA256) using a key derived from a local key file.

Key resolution order:

1. ``NETPILOT_KEY`` environment variable. If it looks like a Fernet key it is used
   directly; otherwise it is treated as a passphrase and stretched with PBKDF2.
2. ``<data-dir>/key`` file (created on first use, mode ``0600``).

Encrypted values are written as ``enc:<token>`` so that a database can be migrated from
plaintext storage without ambiguity.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:"
_SALT = b"netpilot.v1.credential.salt"
_ITERATIONS = 200_000


class SecretError(RuntimeError):
    """Raised when a stored secret cannot be decrypted with the active key."""


def derive_key(material: str | bytes) -> bytes:
    """Return a urlsafe-base64 Fernet key for *material*."""
    if isinstance(material, str):
        material = material.encode()
    # A 32-byte url-safe base64 blob is a Fernet key already.
    try:
        if len(material) == 44:
            base64.urlsafe_b64decode(material)
            return material
    except Exception:  # noqa: BLE001 - not a key, fall through to PBKDF2
        pass
    digest = hashlib.pbkdf2_hmac("sha256", material, _SALT, _ITERATIONS, dklen=32)
    return base64.urlsafe_b64encode(digest)


def load_or_create_key(data_dir: str | os.PathLike[str]) -> bytes:
    """Return the Fernet key for *data_dir*, creating one if necessary."""
    env = os.environ.get("NETPILOT_KEY")
    if env:
        return derive_key(env)

    path = Path(data_dir) / "key"
    if path.exists():
        raw = path.read_bytes().strip()
        if raw:
            return raw
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX filesystems
        pass
    return key


class SecretBox:
    """Symmetric encryption helper bound to a single key."""

    def __init__(self, key: bytes | str) -> None:
        if isinstance(key, str):
            key = key.encode()
        self._fernet = Fernet(key)

    @classmethod
    def for_data_dir(cls, data_dir: str | os.PathLike[str]) -> "SecretBox":
        return cls(load_or_create_key(data_dir))

    def encrypt(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        token = self._fernet.encrypt(value.encode()).decode()
        return PREFIX + token

    def decrypt(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(PREFIX):
            # Value predates encryption (or was written by hand) — treat it as plaintext.
            return value
        try:
            return self._fernet.decrypt(value[len(PREFIX) :].encode()).decode()
        except InvalidToken as exc:  # pragma: no cover - depends on local key state
            raise SecretError(
                "Could not decrypt a stored credential. The key file or NETPILOT_KEY "
                "does not match the one used to save it."
            ) from exc

    @staticmethod
    def generate_token(nbytes: int = 24) -> str:
        return secrets.token_urlsafe(nbytes)
