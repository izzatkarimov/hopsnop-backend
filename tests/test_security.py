import base64
import hashlib

import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)


# --- password hashing ----------------------------------------------------


def test_password_hash_is_argon2id() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert password_hash.startswith("$argon2id$")
    assert "correct horse battery staple" not in password_hash


def test_password_hash_is_salted() -> None:
    assert hash_password("same password") != hash_password("same password")


def test_correct_password_verifies() -> None:
    assert verify_password("s3cret passphrase", hash_password("s3cret passphrase"))


def test_wrong_password_does_not_verify() -> None:
    assert not verify_password("wrong", hash_password("s3cret passphrase"))


@pytest.mark.parametrize(
    "stored", ["", "not-a-real-hash", "5f4dcc3b5aa765d61d8327deb882cf99"]
)
def test_value_that_is_not_an_argon2_hash_never_verifies(stored: str) -> None:
    assert not verify_password("anything", stored)


def test_current_hash_does_not_need_rehash() -> None:
    assert not password_needs_rehash(hash_password("s3cret passphrase"))


def test_hash_with_weaker_parameters_needs_rehash() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

    assert password_needs_rehash(weak.hash("s3cret passphrase"))


def test_dummy_hash_is_a_real_argon2id_hash() -> None:
    # It has to cost as much to verify against as a real account's hash.
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
    assert not password_needs_rehash(DUMMY_PASSWORD_HASH)


# --- tokens --------------------------------------------------------------


def test_token_has_256_bits_of_entropy() -> None:
    token = generate_token()

    assert len(base64.urlsafe_b64decode(token + "=")) == 32


def test_tokens_are_unique() -> None:
    assert len({generate_token() for _ in range(1000)}) == 1000


def test_token_hash_is_sha256_hex() -> None:
    token = generate_token()

    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len(hash_token(token)) == 64


def test_token_hash_is_deterministic_and_not_the_token() -> None:
    token = generate_token()

    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token
    assert hash_token(token) != hash_token(generate_token())


# --- configuration -------------------------------------------------------


PRODUCTION_SECRET = "a-rate-limit-secret-of-sufficient-length"


def make_settings(monkeypatch: pytest.MonkeyPatch, **values: object) -> Settings:
    """Settings built from the given values only, ignoring .env and the shell."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    values.setdefault("database_url", "postgresql+psycopg://unused")
    if values.get("environment", "production") == "production":
        # What production cannot start without; see the tests further down.
        values.setdefault("frontend_url", "https://app.hopsnop.example")
        values.setdefault("rate_limit_secret", PRODUCTION_SECRET)
    return Settings(_env_file=None, **values)


def test_environment_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    assert make_settings(monkeypatch).environment == "production"


def test_unknown_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        make_settings(monkeypatch, environment="staging")


def test_session_cookie_is_secure_unless_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def secure(**values: object) -> bool:
        return make_settings(monkeypatch, **values).session_cookie_secure

    assert secure() is True
    assert secure(environment="production") is True
    assert secure(environment="development") is False


def test_session_cookie_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(monkeypatch)

    assert settings.session_cookie_samesite == "lax"
    # A neutral name that says nothing about the mechanism behind it.
    assert settings.session_cookie_name == "hopsnop_session"


def test_samesite_none_cannot_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        make_settings(monkeypatch, session_cookie_samesite="none")


def test_lifetimes_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(
        monkeypatch,
        session_lifetime_days=7,
        session_last_used_interval_seconds=60,
        email_verification_token_lifetime_hours=2,
        password_reset_token_lifetime_minutes=10,
    )

    assert settings.session_lifetime.days == 7
    assert settings.session_last_used_interval.total_seconds() == 60
    assert settings.email_verification_token_lifetime.total_seconds() == 2 * 3600
    assert settings.password_reset_token_lifetime.total_seconds() == 10 * 60


@pytest.mark.parametrize(
    "name",
    [
        "session_lifetime_days",
        "email_verification_token_lifetime_hours",
        "password_reset_token_lifetime_minutes",
    ],
)
def test_lifetimes_must_be_positive(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(monkeypatch, **{name: 0})


def test_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setenv("SESSION_LIFETIME_DAYS", "3")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.delenv("RATE_LIMIT_SECRET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.session_lifetime.days == 3
    assert settings.is_development


def test_password_minimum_length_cannot_be_configured_below_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert make_settings(monkeypatch).password_min_length == 12
    with pytest.raises(ValidationError):
        make_settings(monkeypatch, password_min_length=4)


def test_frontend_origin_is_derived_from_frontend_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(monkeypatch, frontend_url="https://App.Hopsnop.example/")

    assert settings.frontend_url == "https://App.Hopsnop.example"
    assert settings.frontend_origin == "https://app.hopsnop.example"


@pytest.mark.parametrize("url", ["app.hopsnop.example", "ftp://hopsnop.example", ""])
def test_frontend_url_must_be_an_absolute_http_url(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    with pytest.raises(ValidationError):
        make_settings(monkeypatch, frontend_url=url)
