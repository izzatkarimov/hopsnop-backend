"""Password hashing and secret-token primitives.

Two kinds of secret are handled here, and they are hashed differently on
purpose.

Passwords are chosen by people and are low-entropy, so they are hashed with
Argon2id: salted, and deliberately slow and memory-hard to make offline
guessing expensive.

Session, email-verification and password-reset tokens are 256 random bits
that this application generates. Guessing one is infeasible however fast the
hash is, so they are hashed with plain SHA-256. A slow or salted hash would
add nothing, and a deterministic hash is what allows a presented token to be
looked up by its hash. Only the hash is stored; the raw token exists in the
cookie or link that was handed out and nowhere else.
"""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Library defaults: Argon2id with the RFC 9106 low-memory profile. Each hash
# gets its own random salt, and the salt and parameters are encoded in the
# hash string.
_password_hasher = PasswordHasher()

# Upper bound on accepted passwords, so that a request cannot make the server
# hash an arbitrarily large input.
PASSWORD_MAX_LENGTH = 128

_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        # Wrong password, or a stored value that is not an Argon2 hash.
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with parameters weaker than the current ones."""
    return _password_hasher.check_needs_rehash(password_hash)


# Verified against when a login names an account that does not exist, so that
# the response takes as long as it does for a real account.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(_TOKEN_BYTES))


def generate_token() -> str:
    """A new URL-safe secret token with 256 bits of entropy from the OS CSPRNG."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The form in which a token is stored and looked up: its SHA-256 hex digest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
