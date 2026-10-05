from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str

    # "production" is the default on purpose: a deployment that forgets to set
    # ENVIRONMENT gets the strict behaviour. The development conveniences (a
    # session cookie that works over plain HTTP, email links written to the
    # log) have to be switched on explicitly with ENVIRONMENT=development.
    environment: Literal["development", "production"] = "production"

    # Where the web frontend is served. It is the base of the links sent by
    # email, and its origin is the only other origin allowed to make
    # credentialed browser requests to this API (CORS and the CSRF check).
    frontend_url: str = "http://localhost:3000"

    # --- session cookie ---------------------------------------------------
    session_cookie_name: str = "hopsnop_session"
    # "none" is deliberately not an option: the CSRF defence relies on the
    # browser withholding the cookie from cross-site requests.
    session_cookie_samesite: Literal["lax", "strict"] = "lax"

    # --- lifetimes --------------------------------------------------------
    session_lifetime_days: int = Field(default=30, gt=0)
    # sessions.last_used_at is written at most once per interval, so that an
    # authenticated request does not normally cost a database write.
    session_last_used_interval_seconds: int = Field(default=300, ge=0)
    email_verification_token_lifetime_hours: int = Field(default=24, gt=0)
    password_reset_token_lifetime_minutes: int = Field(default=30, gt=0)

    # --- password policy --------------------------------------------------
    # Length is the only rule. The floor keeps configuration from weakening
    # the policy into meaninglessness.
    password_min_length: int = Field(default=12, ge=8)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("frontend_url")
    @classmethod
    def _validate_frontend_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value.rstrip("/")

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def frontend_origin(self) -> str:
        """The frontend's origin in the form browsers send it: scheme://host[:port]."""
        parts = urlsplit(self.frontend_url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    @property
    def session_cookie_secure(self) -> bool:
        # Browsers do not send Secure cookies over plain HTTP, which is what
        # local development uses. Everywhere else the flag is always set and
        # cannot be turned off through configuration.
        return not self.is_development

    @property
    def session_lifetime(self) -> timedelta:
        return timedelta(days=self.session_lifetime_days)

    @property
    def session_last_used_interval(self) -> timedelta:
        return timedelta(seconds=self.session_last_used_interval_seconds)

    @property
    def email_verification_token_lifetime(self) -> timedelta:
        return timedelta(hours=self.email_verification_token_lifetime_hours)

    @property
    def password_reset_token_lifetime(self) -> timedelta:
        return timedelta(minutes=self.password_reset_token_lifetime_minutes)


settings = Settings()
