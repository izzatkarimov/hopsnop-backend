# Hopsnop Backend

FastAPI backend for Hopsnop, a secure social media platform.

## Setup

```bash
cp .env.example .env
docker compose up -d                      # PostgreSQL on localhost:5433
pip install -r requirements-dev.txt       # runtime + test dependencies
alembic upgrade head                      # create the schema
uvicorn app.main:app --reload
```

## Database

The schema is defined by the SQLAlchemy models in `app/models/` and managed
with Alembic. It currently consists of `users`, `posts`, `likes`, `reposts`,
`follows`, `stories`, `story_views`, `sessions`, `email_verification_tokens`
and `password_reset_tokens`.

```bash
alembic upgrade head                              # apply migrations
alembic current                                   # show the applied revision
alembic check                                     # fail if models and schema differ
alembic revision --autogenerate -m "message"      # new migration; review it by hand
```

Deletion rules:

- Posts and stories block deletion of their author (`ON DELETE RESTRICT`).
  Accounts are deactivated through `users.is_active`, not deleted.
- Likes, reposts, follows and story views are removed together with the user,
  post or story they refer to (`ON DELETE CASCADE`).
- Sessions and email verification / password reset tokens are removed together
  with their user (`ON DELETE CASCADE`). Deactivating an account keeps them,
  but makes them unusable.

## Configuration

Settings are read from the environment or `.env` (see `.env.example`) by
`app/core/config.py`, which is the only place authentication parameters are
defined.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | required | SQLAlchemy URL of the PostgreSQL database |
| `ENVIRONMENT` | `production` | `development` or `production`, see below |
| `FRONTEND_URL` | `http://localhost:3000` | Base of emailed links; the only other origin allowed to call the API from a browser |
| `SESSION_COOKIE_NAME` | `hopsnop_session` | Name of the session cookie |
| `SESSION_COOKIE_SAMESITE` | `lax` | `lax` or `strict`; `none` is not accepted |
| `SESSION_LIFETIME_DAYS` | `30` | How long a session lasts after login |
| `SESSION_LAST_USED_INTERVAL_SECONDS` | `300` | Minimum time between writes of `sessions.last_used_at` |
| `EMAIL_VERIFICATION_TOKEN_LIFETIME_HOURS` | `24` | Validity of a verification link |
| `PASSWORD_RESET_TOKEN_LIFETIME_MINUTES` | `30` | Validity of a password reset link |
| `PASSWORD_MIN_LENGTH` | `12` | Minimum password length (cannot be set below 8) |

`ENVIRONMENT` defaults to `production` so that a deployment which forgets to
set it gets the strict behaviour. `development` changes exactly two things:

- the session cookie is sent without `Secure`, because browsers do not return
  `Secure` cookies over plain HTTP;
- verification and password reset links are written to the server log instead
  of being emailed.

## Authentication

Database-backed sessions carried in an `HttpOnly` cookie. There are no JWTs and
nothing for frontend JavaScript to store.

| Endpoint | Purpose |
| --- | --- |
| `POST /auth/register` | Create an unverified account and send a verification link |
| `POST /auth/verify-email` | Redeem a verification token |
| `POST /auth/resend-verification` | Send a new verification link |
| `POST /auth/login` | Check credentials, start a session, set the cookie |
| `POST /auth/logout` | Revoke the current session, clear the cookie |
| `GET /auth/me` | The authenticated user |
| `POST /auth/forgot-password` | Send a password reset link |
| `POST /auth/reset-password` | Redeem a reset token and set a new password |
| `GET /auth/sessions` | The user's active sessions |
| `DELETE /auth/sessions/{id}` | Revoke one of the user's sessions |
| `POST /auth/sessions/revoke-others` | Revoke every session except the current one |

The flow is registration, then email verification, then login. Registration
does not log the user in, and an account cannot log in until its email address
is verified.

Other routers require authentication by depending on `CurrentUser` (or
`CurrentSession`) from `app/api/deps.py`. That dependency is the only code that
decides whether a request is authenticated.

### Passwords

Passwords are hashed with Argon2id (`argon2-cffi`, RFC 9106 parameters), with a
random salt per hash. The only policy is a length between `PASSWORD_MIN_LENGTH`
and 128 characters. Hashes made with older parameters are upgraded at the next
successful login.

### Sessions and tokens

A session token is 256 random bits from the operating system's CSPRNG. The
browser gets the token in the cookie; the database stores only its SHA-256
digest, so a copy of the database does not contain usable session credentials.
Verification and reset tokens work the same way. SHA-256 is appropriate here,
and would not be for passwords, because these values are random and cannot be
guessed however fast the hash is.

A session is accepted only while it is not revoked, not expired, and its user
is active. Logout, password reset and the session endpoints revoke sessions by
setting `revoked_at`; rows are not deleted. A password reset revokes all of the
user's sessions.

`last_used_at` is updated at most once per
`SESSION_LAST_USED_INTERVAL_SECONDS`, so it can lag behind the real last use by
up to that interval. This keeps ordinary authenticated requests from writing to
the database. Using a session does not extend its expiry.

Verification and reset tokens are single-use and expire. Issuing a new one
deletes the user's previous unused one, so only the latest link works and
`used_at` always means the token was redeemed.

### Account enumeration

Login gives the same `401` for an unknown account, a wrong password and a
deactivated account, and verifies a dummy hash when the account does not exist
so that timing does not distinguish the cases either. `resend-verification` and
`forgot-password` always answer `202` with the same body. Registration does
report a username or email that is already taken.

### Cookie and CSRF

The cookie is `HttpOnly`, `SameSite=Lax`, `Path=/`, host-only (no `Domain`), and
`Secure` outside development. Because a cookie is attached automatically,
`HttpOnly` does nothing against CSRF; three things do:

1. `SameSite=Lax`: the browser does not attach the cookie to cross-site `POST`
   or `DELETE` requests.
2. Origin verification (`verify_request_origin`, applied to every route): a
   state-changing request whose `Origin` (or, failing that, `Referer`) is not
   the frontend's origin or the API's own origin is rejected with `403`.
   Requests with neither header are not browser cross-site requests and are
   allowed.
3. CORS allows credentialed requests from `FRONTEND_URL`'s origin only, so
   other sites cannot read responses or send preflighted JSON requests.

`GET` requests never change state. This design needs no CSRF token as long as
the frontend and the API are on the same site (for example `app.example.com`
and `api.example.com`, or `localhost:3000` and `localhost:8000`). If they are
ever deployed on unrelated domains the cookie would need `SameSite=None`, and a
CSRF token would have to be added first.

When the API runs behind a TLS-terminating proxy, the server must be told the
original scheme (uvicorn's `--proxy-headers` and `--forwarded-allow-ips`), or
same-origin requests such as those from `/docs` are rejected.

### Email

There is no email provider yet. `app/services/email.py` defines the
`EmailSender` interface; in development the links are logged, and in production
nothing is sent, so accounts cannot be verified in production until a provider
is added there.

To verify an account locally, register, then copy the token from the
`[development] Email verification link` line in the server log:

```bash
curl -X POST localhost:8000/auth/verify-email \
     -H 'Content-Type: application/json' -d '{"token": "<token>"}'
```

## Tests

```bash
pytest
```

The tests use the database from `DATABASE_URL` and need the migrations applied.
Each test runs in a transaction that is rolled back, so nothing is committed.
The API tests run the application inside that same transaction and always use
the production settings, whatever `ENVIRONMENT` is set to locally.

Several tests assume empty tables, so they can fail if the development database
contains accounts with the usernames the tests use (`alice`, `bob`).
