"""Symmetric encryption for OAuth tokens at rest.

We never store provider refresh tokens in plaintext. The Fernet key is held in
TOKEN_ENC_KEY env var. Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _key() -> bytes:
    raw = os.getenv("TOKEN_ENC_KEY", "")
    if not raw:
        raise RuntimeError(
            "TOKEN_ENC_KEY env var not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    # Accept either a proper Fernet key or any string (we'll derive a key from it).
    try:
        Fernet(raw.encode())
        return raw.encode()
    except Exception:
        digest = hashlib.sha256(raw.encode()).digest()
        return base64.urlsafe_b64encode(digest)


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    f = Fernet(_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    f = Fernet(_key())
    return f.decrypt(ciphertext.encode()).decode()
