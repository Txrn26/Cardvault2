"""
Password hashing and session helpers. No new dependency -- PBKDF2-SHA256
via Python's standard hashlib is a well-vetted, still-recommended choice
(this is literally what Django's default hasher uses), not something
rolled from scratch.
"""

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> tuple[str, str]:
    """Returns (salt_hex, hash_hex). Store both."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return salt, digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(candidate.hex(), hash_hex)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)
