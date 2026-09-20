"""Credential encryption."""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

from netpilot.security import PREFIX, SecretBox, SecretError, derive_key, load_or_create_key


def test_key_file_is_created_with_restrictive_permissions(tmp_path):
    key = load_or_create_key(tmp_path)
    assert len(key) == 44  # urlsafe base64 of 32 bytes
    key_path = tmp_path / "key"
    assert key_path.exists()
    if os.name == "posix":
        assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_key_is_stable_across_calls(tmp_path):
    assert load_or_create_key(tmp_path) == load_or_create_key(tmp_path)


def test_env_var_takes_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("NETPILOT_KEY", "a-passphrase")
    first = load_or_create_key(tmp_path)
    monkeypatch.setenv("NETPILOT_KEY", "a-different-passphrase")
    second = load_or_create_key(tmp_path)
    assert first != second


def test_derive_key_accepts_a_ready_made_fernet_key():
    key = Fernet.generate_key()
    assert derive_key(key) == key
    assert derive_key(key.decode()) == key


def test_derive_key_stretches_a_passphrase():
    key = derive_key("hunter2")
    assert len(key) == 44
    assert key == derive_key("hunter2")
    assert key != derive_key("hunter3")


def test_roundtrip():
    box = SecretBox(Fernet.generate_key())
    for value in ("secret", "with spaces", "ünïcode", "a" * 5000):
        token = box.encrypt(value)
        assert token is not None
        assert token.startswith(PREFIX)
        assert value not in token
        assert box.decrypt(token) == value


def test_empty_values_stay_empty():
    box = SecretBox(Fernet.generate_key())
    assert box.encrypt(None) is None
    assert box.encrypt("") is None
    assert box.decrypt(None) is None
    assert box.decrypt("") is None


def test_plaintext_values_are_passed_through():
    """A database written before encryption was added should keep working."""
    box = SecretBox(Fernet.generate_key())
    assert box.decrypt("legacy-plaintext") == "legacy-plaintext"


def test_wrong_key_is_reported_clearly():
    token = SecretBox(Fernet.generate_key()).encrypt("secret")
    other = SecretBox(Fernet.generate_key())
    with pytest.raises(SecretError) as excinfo:
        other.decrypt(token)
    assert "does not match" in str(excinfo.value)


def test_generated_tokens_differ():
    assert SecretBox.generate_token() != SecretBox.generate_token()
