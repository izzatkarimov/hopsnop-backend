from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# What the frontend's origin and the rate-limit secret are when nothing is
# configured, in development only. Production refuses to start with either.
_DEVELOPMENT_FRONTEND_URL = "http://localhost:3000"
_DEVELOPMENT_RATE_LIMIT_SECRET = "development-only-rate-limit-secret"
_RATE_LIMIT_SECRET_MIN_LENGTH = 32
# How many times larger the limit on failed logins for an account must be
# than the limit for one address at that account. See the settings.
_ACCOUNT_TO_ADDRESS_LIMIT_RATIO = 3


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
    # Because it is trusted that far it has no default outside development:
    # production must name it, and with https.
    frontend_url: str | None = None

    # --- session cookie ---------------------------------------------------
    # The name without its prefix; see ``session_cookie``.
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

    # --- rate limiting ----------------------------------------------------
    # Key of the HMAC under which rate-limit counters are stored, so that the
    # table holds neither addresses nor identifiers, and nothing that could
    # be turned back into them without this secret. Required in production.
    rate_limit_secret: SecretStr | None = None
    # One window for every counter below. A counter starts with the first
    # request it counts and is back at zero this long after that.
    rate_limit_window_minutes: int = Field(default=15, gt=0)
    # Failed logins. One address guessing at one account is stopped after a
    # handful. The per-account limit is for guesses spread over addresses.
    # The per-address limit is for one address trying many accounts.
    #
    # The per-account limit must be at least three times the first one (it
    # is checked below). The windows of the two counters are not aligned, so
    # one address can fit the end of one of its windows and the start of the
    # next into a single window of the account: up to twice its own limit.
    # At three times, an address doing its worst still leaves the account's
    # owner as many attempts as any address gets, so it takes more than one
    # address to lock an owner out.
    login_failures_per_identifier_and_ip: int = Field(default=5, gt=0)
    login_failures_per_identifier: int = Field(default=20, gt=0)
    login_failures_per_ip: int = Field(default=30, gt=0)
    # Wrong current passwords when changing a password, per account.
    password_change_failures: int = Field(default=5, gt=0)
    # Requests per address, whatever their outcome.
    registrations_per_ip: int = Field(default=5, gt=0)
    # For /auth/forgot-password and /auth/resend-verification, each.
    email_requests_per_ip: int = Field(default=5, gt=0)
    # For /auth/verify-email and /auth/reset-password, each.
    token_redemptions_per_ip: int = Field(default=10, gt=0)
    # How long after a verification or reset link was issued another request
    # for the same account is quietly not acted on, so that the link in the
    # owner's inbox cannot be made useless by asking again.
    email_token_cooldown_seconds: int = Field(default=300, ge=0)

    # --- requests ---------------------------------------------------------
    # The largest request body that is read. Every body this API takes is a
    # small JSON object (the largest holds a URL of up to 2083 characters),
    # so this leaves a wide margin.
    max_request_body_bytes: int = Field(default=64 * 1024, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A setting that is refused is named, not shown: the error ends up in
        # the startup log, and the values include the database URL and the
        # rate-limit secret.
        hide_input_in_errors=True,
    )

    @field_validator("frontend_url")
    @classmethod
    def _validate_frontend_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an absolute http(s) URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _require_safe_settings(self) -> "Settings":
        """Refuse limits that defeat each other, in any environment; then
        fill in the development defaults, or refuse an unsafe production.

        Checked once, when the settings are loaded, which is when the
        application starts: a production that is misconfigured does not come
        up at all rather than run with a trust it was never meant to have.
        """
        pair, account = (
            self.login_failures_per_identifier_and_ip,
            self.login_failures_per_identifier,
        )
        if account < _ACCOUNT_TO_ADDRESS_LIMIT_RATIO * pair:
            raise ValueError(
                "LOGIN_FAILURES_PER_IDENTIFIER must be at least "
                f"{_ACCOUNT_TO_ADDRESS_LIMIT_RATIO} times "
                "LOGIN_FAILURES_PER_IDENTIFIER_AND_IP"
            )

        if self.is_development:
            if self.frontend_url is None:
                self.frontend_url = _DEVELOPMENT_FRONTEND_URL
            if self.rate_limit_secret is None:
                self.rate_limit_secret = SecretStr(_DEVELOPMENT_RATE_LIMIT_SECRET)
            return self

        if self.frontend_url is None:
            raise ValueError("FRONTEND_URL must be set in production")
        if urlsplit(self.frontend_url).scheme != "https":
            raise ValueError("FRONTEND_URL must be an https URL in production")
        secret = self.rate_limit_secret
        if secret is None:
            raise ValueError("RATE_LIMIT_SECRET must be set in production")
        if len(secret.get_secret_value()) < _RATE_LIMIT_SECRET_MIN_LENGTH:
            raise ValueError(
                "RATE_LIMIT_SECRET must be at least "
                f"{_RATE_LIMIT_SECRET_MIN_LENGTH} characters long"
            )
        if secret.get_secret_value() == _DEVELOPMENT_RATE_LIMIT_SECRET:
            raise ValueError("RATE_LIMIT_SECRET must not be the development value")
        return self

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
    def session_cookie(self) -> str:
        """The name the session cookie is set and read under.

        Outside development it carries the ``__Host-`` prefix. A browser only
        accepts a cookie of such a name if it is Secure, has Path=/ and has
        no Domain, so no other host of the same site (a sibling subdomain,
        say) can plant a session cookie that this API would read.
        """
        if self.session_cookie_secure:
            return f"__Host-{self.session_cookie_name}"
        return self.session_cookie_name

    @property
    def api_docs_enabled(self) -> bool:
        # The interactive documentation and the schema describe every route.
        # They are a development tool and are not served anywhere else.
        return self.is_development

    @property
    def rate_limit_window(self) -> timedelta:
        return timedelta(minutes=self.rate_limit_window_minutes)

    @property
    def email_token_cooldown(self) -> timedelta:
        return timedelta(seconds=self.email_token_cooldown_seconds)

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
