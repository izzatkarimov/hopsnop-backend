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
`follows`, `stories`, `story_views`, `sessions`, `email_verification_tokens`,
`password_reset_tokens` and `rate_limits`.

```bash
alembic upgrade head                              # apply migrations
alembic current                                   # show the applied revision
alembic check                                     # fail if models and schema differ
alembic revision --autogenerate -m "message"      # new migration; review it by hand
```

Deletion rules:

- Posts and stories block deletion of their author (`ON DELETE RESTRICT`).
  An account's row can therefore only be removed once its posts have been
  erased and its stories removed, which is what
  [deleting an account](#deleting-an-account) does, in that order. The only
  other thing that removes a user is the [retention purge](#retention), for
  accounts that were never verified and only where that is switched on, and
  it does so by the same code.
  Nothing can leave a post attributed to an account that is gone.
- A deleted post is erased, see [Deletion](#deletion). If nobody replied to
  it its row is removed. A reply blocks removal of the row it replies to
  (`ON DELETE RESTRICT` on `posts.parent_post_id`), so a reply never loses
  its parent: a deleted post that has replies is kept as a tombstone, a row
  without author and text. A check constraint (`ck_posts_live_or_erased`)
  allows a row exactly two states, a post (author, text, no `deleted_at`) or
  a tombstone (no author, no text, `deleted_at`), and nothing in between.
- Likes, reposts, follows and story views are removed together with the user,
  post or story they refer to (`ON DELETE CASCADE`).
- Sessions and email verification / password reset tokens are removed together
  with their user (`ON DELETE CASCADE`). Deactivating an account keeps them,
  but makes them unusable.

Every transaction runs at READ COMMITTED. That is PostgreSQL's default, and it
is set explicitly on the engine (`app/db/session.py`) because the application
depends on it: a request that has waited for a row lock goes on with the row
as the holder of the lock left it. The locks on an account's row, on tokens
and on rate-limit counters are all built on that, so the level is not left to
however the server happens to be configured.

## Configuration

Settings are read from the environment or `.env` (see `.env.example`) by
`app/core/config.py`, which is the only place authentication parameters are
defined.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | required | SQLAlchemy URL of the PostgreSQL database |
| `ENVIRONMENT` | `production` | `development` or `production`, see below |
| `FRONTEND_URL` | development only: `http://localhost:3000` | Base of emailed links; the only other origin allowed to call the API from a browser |
| `RATE_LIMIT_SECRET` | development only | Key of the hashes that rate-limit counters are stored under |
| `SESSION_COOKIE_NAME` | `hopsnop_session` | Name of the session cookie, before its `__Host-` prefix |
| `SESSION_COOKIE_SAMESITE` | `lax` | `lax` or `strict`; `none` is not accepted |
| `SESSION_LIFETIME_DAYS` | `30` | How long a session lasts after login |
| `SESSION_LAST_USED_INTERVAL_SECONDS` | `300` | Minimum time between writes of `sessions.last_used_at` |
| `EMAIL_VERIFICATION_TOKEN_LIFETIME_HOURS` | `24` | Validity of a verification link |
| `PASSWORD_RESET_TOKEN_LIFETIME_MINUTES` | `30` | Validity of a password reset link |
| `PASSWORD_MIN_LENGTH` | `12` | Minimum password length (cannot be set below 8) |
| `MAX_REQUEST_BODY_BYTES` | `65536` | Largest request body that is read |

The limits of [rate limiting](#rate-limiting) and the periods of
[retention](#retention) are settings as well and are listed there.

`ENVIRONMENT` defaults to `production` so that a deployment which forgets to
set it gets the strict behaviour. `development` changes these things and no
others:

- the session cookie is sent without `Secure` and without the `__Host-`
  prefix, because browsers do not return `Secure` cookies over plain HTTP;
- verification and password reset links are written to the server log instead
  of being emailed;
- `FRONTEND_URL` and `RATE_LIMIT_SECRET` have defaults;
- the interactive API documentation is served.

### Production

A production does not start unless it is configured safely. The settings are
checked once, when the application is loaded, and it fails there if

- `FRONTEND_URL` is not set, or is not an `https` URL. That origin is trusted
  with credentialed requests, so there is no fallback to `localhost`;
- `RATE_LIMIT_SECRET` is not set, is shorter than 32 characters, or is the
  development value.

`/docs`, `/redoc` and `/openapi.json` are not served in production (`404`).

Three things are left to the deployment, because this application cannot do
them properly: TLS and the `Strict-Transport-Security` header belong to
whatever terminates TLS; the Content-Security-Policy of the pages belongs to
the frontend; and the server has to be told the client's address by the proxy
in front of it (see [rate limiting](#rate-limiting)).

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
| `POST /auth/change-password` | Set a new password, knowing the current one |
| `POST /auth/delete-account` | Remove the account and everything of it, knowing the password |
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

`POST /auth/change-password` takes `current_password` and `new_password` from
a signed-in, verified user. A wrong current password is a `400`, and such
failures are limited per account like failed logins. On success:

- the new password is hashed afresh and the old one stops working;
- every other session of the user is revoked;
- the current session continues, but under a new token, sent in a new cookie.
  A copy of the old cookie is therefore worth nothing either. The session
  keeps its id and its expiry;
- a password reset link that is still outstanding is withdrawn.

A login, a password change and a password reset for one account pass through
a lock on the account's row, one at a time. A login checks the password
first, which takes a while, and then takes the lock and reads the stored hash
again: if the password was changed or reset in the meantime it is refused
like any wrong password, and no session is created. A login that got there
first is waited for, and its session is ended with the others. Either way no
session that was opened with the old password is left once the change or
reset is done. Two changes of the same password at once cannot both take
effect for the same reason: the second finds that the password it proved is
no longer the current one.

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
user's sessions. Deleting the account removes the rows themselves, and the
[retention purge](#retention) removes those of sessions that ended some time
ago.

`last_used_at` is updated at most once per
`SESSION_LAST_USED_INTERVAL_SECONDS`, so it can lag behind the real last use by
up to that interval. This keeps ordinary authenticated requests from writing to
the database. Using a session does not extend its expiry.

Verification and reset tokens are single-use and expire. Issuing a new one
deletes the user's previous unused one, so only the latest link works and
`used_at` always means the token was redeemed. A new one is not issued within
`EMAIL_TOKEN_COOLDOWN_SECONDS` of the last (see
[rate limiting](#rate-limiting)).

### Deleting an account

`POST /auth/delete-account` removes the signed-in user's account, at once and
for good. There is no grace period and nothing to undo it with.

| | |
| --- | --- |
| Body | `{"password": "..."}`, the current password. Nothing in the request says whose account it is: it is the session's |
| Needs | A session. A verified email address is not required to leave |
| `204` | Removed. No body; the session cookie is cleared |
| `400` | Wrong password (`Current password is incorrect.`), or the password was changed or reset while the request was under way |
| `401` | No session, or the account was removed while the request was under way |
| `403` | Sent from another site |
| `422` | No usable password in the body |
| `429` | Too many wrong passwords for this account, see [rate limiting](#rate-limiting) |

**What is removed.** Everything that is the account's, in one transaction,
all of it or none of it:

| Data | What becomes of it |
| --- | --- |
| The account's row: username, email, display name, bio, avatar, password hash | Removed |
| Sessions, all of them | Removed (not revoked: the rows) |
| Verification and reset tokens, used or not | Removed. A link still in a mailbox opens nothing |
| Posts that nobody replied to | Removed, with their likes and reposts |
| Posts that have replies | Erased like any [deleted post](#deletion): a tombstone without author and text, likes and reposts removed |
| Likes and reposts the account made | Removed |
| Follows, in both directions | Removed |
| Stories, expired ones included, and who viewed them | Removed |
| Views the account made of others' stories | Removed |
| Rate-limit counters under its username, its email and its id (wrong passwords, exports) | Removed |

What is left is the tombstones, and only where somebody else's reply needs
one to stay in its thread. A tombstone holds an id, the id of the post it
answered and timestamps; it holds no author, no text, and nothing else. It
goes when the last reply to it goes. Replies by other people are not touched
and keep their `parent_post_id`.

The username and the email address can be registered again from the moment
the deletion is committed. Nothing is kept that remembers them, so whoever
takes them next gets a new account that has nothing to do with the old one:
no followers, no posts, no failed logins counted against the name.

The tests check this as stated: after a deletion every column of every table
that holds an id is searched for the account's id, and every column that
holds text for its username, email, display name, bio, avatar, password hash,
token hashes and everything it wrote (`tests/test_account_deletion.py`).

**Order.** The request counts the attempt against the limit, checks the
password (Argon2, with no lock held), and then, in one transaction:

1. locks the account's row `FOR UPDATE` and reads the password hash again;
2. takes the erasure lock;
3. locks the account's posts `FOR UPDATE`, in the order of their ids;
4. removes the likes and reposts of those posts, turns them into tombstones,
   and removes every tombstone that has no reply, upwards through the threads;
5. removes the account's stories (their views go with them);
6. removes the account's row; the foreign keys remove its sessions, tokens,
   follows, likes, reposts and story views;
7. commits.

Then the counters kept under the account's names are removed and the cookie
is cleared. The number of statements does not depend on how much the account
holds.

**Other requests at the same moment.** Every request that involves an
account's row takes that row first, and in one of three strengths:

| Lock on the account's row | Taken by |
| --- | --- |
| `FOR KEY SHARE` | Whatever writes a row in the account's name: a post, a reply, a like, a repost, a follow, a story, a view (`hold_account`) |
| `FOR NO KEY UPDATE` | A login, a password change or reset, a link being issued or redeemed, a [data export](#data-export) |
| `FOR UPDATE` | Deleting the account |

The first two do not get in each other's way. A deletion excludes both, so it
and any of them come one after the other, and whichever is second sees what
the first has done:

| The other request | If it comes first | If the deletion comes first |
| --- | --- | --- |
| Login with the password | The deletion waits; the new session is removed with the rest | `401`, no session is created |
| Password change | Deletion `400`: the password it was given is no longer the account's. Nothing is removed | `401` |
| Password reset | Deletion `400`, as above | `400`, the link is gone with the account |
| Email verification | The deletion waits, then removes the account | `400`, the link is gone |
| Request for a link | The deletion waits; the link is removed | `202` as always, no link is issued |
| A second deletion | | `401` |
| The account's own post, reply, like, repost, follow, story, view | The deletion waits and removes what was written | `401`, nothing is written |
| The account's own profile change | The deletion waits | `401` |
| Somebody following the account | The deletion waits; the follow is removed | `404` |
| Somebody replying to one of its posts | The deletion waits; the post stays as a tombstone for the reply | `404` |
| Somebody liking or reposting one of its posts, or viewing its story | The deletion waits; the like or view is removed | `404` |
| Registration of the same username or email | `409` until the deletion is committed | `201` |

A request that was let in before the deletion and is answered after it may
find that what it would answer with is gone: a login or a new post that came
first can be answered `401`, since by then it is a request without a session.
Nothing it wrote is left either way, and no interleaving produces a `500`.

Writers take their own account's row before anything else because the
deletion does. A reply holds the post it answers, and a deletion of the
replier's account holds the account and then wants that post if it is the
account's own; taken in opposite orders the two would wait for each other.
The full order of locks is in `app/services/erasure.py`.

**Measured** on 20,000 accounts, 300,000 posts and 200,000 likes:

| Operation | Time |
| --- | --- |
| Checking the password (part of every request) | 33 ms |
| Delete an account that holds nothing, through the API | 50 ms median |
| Delete an ordinary account (200 posts, 500 likes, 300 follows, 5 stories), through the API | 71 ms median |
| The same, the transaction alone (how long the locks are held) | 28 ms median |
| Account with 1,000 / 10,000 / 100,000 posts, half of them answered, the transaction alone | 67 ms / 0.42 s / 3.6 s |
| Other users reading, posting, replying, liking, following while such a deletion holds its locks | unchanged (6 to 12 ms) |
| A login to the account, or another user's post deletion, during a deletion | waits until it is committed, and no longer |
| A deletion while the account is writing something for 100 ms / 500 ms | 117 ms / 524 ms |
| What holding its own account first adds to a post, a like or a follow | 0.5 ms (one statement) |

While an account is being deleted, two things wait: requests of that same
account, and other erasures (a post being deleted elsewhere), which take
their turn at the erasure lock. For an ordinary account that is some tens of
milliseconds. No index was added: every row of an account is found through
an index that was already there.

**What a deletion does not reach.** Counters of the rate limiter that are
keyed by an address alone, or by the account's name together with an address
other than the one the deletion came from, cannot be found from the account
and stay until the [retention purge](#retention) removes them, once their
window is over. They hold a keyed hash and a number, no name and no address. Database backups, the write-ahead log and server logs are
outside the application.

### Account enumeration

Login gives the same `401` for an unknown account, a wrong password and a
deactivated account, and verifies a dummy hash when the account does not exist
so that timing does not distinguish the cases either. `resend-verification` and
`forgot-password` always answer `202` with the same body, also within the
cooldown on links. Registration does report a username or email that is
already taken. It is limited per address, which slows such questions down but
does not prevent them; answering every registration alike needs working
email first and is deferred.

### Cookie and CSRF

The cookie is `HttpOnly`, `SameSite=Lax`, `Path=/`, host-only (no `Domain`), and
`Secure` outside development. Outside development its name carries the
`__Host-` prefix (`__Host-hopsnop_session`). Browsers accept a cookie of such a
name only with exactly those attributes, so another host of the same site
cannot plant a session cookie for this one. Because a cookie is attached
automatically, `HttpOnly` does nothing against CSRF; three things do:

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

### Rate limiting

The endpoints that check a secret or send an email are rate limited. A request
over a limit is answered with `429` and a `Retry-After` header, the same
answer whichever limit it was. The header is exposed to the frontend
(`Access-Control-Expose-Headers`), so its scripts can read it.

| Endpoint | Counted | Per | Setting | Default |
| --- | --- | --- | --- | --- |
| `POST /auth/login` | failures | identifier and address | `LOGIN_FAILURES_PER_IDENTIFIER_AND_IP` | 5 |
| | failures | identifier | `LOGIN_FAILURES_PER_IDENTIFIER` | 20 |
| | failures | address | `LOGIN_FAILURES_PER_IP` | 30 |
| `POST /auth/change-password`, `/auth/delete-account`, `/users/me/export` | failures | account | `PASSWORD_CONFIRMATION_FAILURES` | 5 together |
| `POST /users/me/export` | exports | account | `DATA_EXPORTS_PER_ACCOUNT` | 3 |
| `POST /auth/register` | requests | address | `REGISTRATIONS_PER_IP` | 5 |
| `POST /auth/forgot-password`, `/auth/resend-verification` | requests | address | `EMAIL_REQUESTS_PER_IP` | 5 each |
| `POST /auth/verify-email`, `/auth/reset-password` | requests | address | `TOKEN_REDEMPTIONS_PER_IP` | 10 each |

All of them count within one window, `RATE_LIMIT_WINDOW_MINUTES` (15). A
window starts with the first request counted and ends that long after;
being refused does not extend it.

`PASSWORD_CONFIRMATION_FAILURES` is the limit on wrong passwords where a
signed-in user is asked for the password again. It is counted per account and
not per operation: whatever asks for the password counts against the same
counter, so a session in the wrong hands gets that many guesses in all.

A login attempt takes a place in each counter before the password is looked
at, and gives the places back if the login succeeds. So a right password is
refused as well while a limit is reached, no password is hashed for a refused
request, and attempts made at the same moment cannot outnumber the limit. A
request that finds a counter full takes no place in any of them: what the
counters hold are wrong passwords, and attempts still under way. For that
last reason more simultaneous attempts than a counter has places are refused
for the moment, whatever they would have turned out to be, and leave nothing
behind.

The counters are keyed by the identifier as typed, so an identifier that
names no account is limited exactly like one that does, and a username and
an email address of one account are counted apart.

One address is stopped after 5 failures for an account, and from then on adds
nothing to that account's limit of 20, which is there for guesses spread over
several addresses. The windows of the two counters are not aligned, so an
address can fit the end of one of its windows and the start of the next into
a single window of the account: up to 10 failures, not 5. That is why
`LOGIN_FAILURES_PER_IDENTIFIER` must be at least three times
`LOGIN_FAILURES_PER_IDENTIFIER_AND_IP`; the application does not start
otherwise. One address can therefore never lock an account's owner out. Two
or more can, for the rest of the window. That is the price of limiting such
an attack at all.

**Emailed links.** Besides the limit per address, a verification or reset
link is not replaced within `EMAIL_TOKEN_COOLDOWN_SECONDS` (300) of being
issued: the request is answered with the usual `202`, but no new token is
made and the link already sent stays valid. Without this, anyone who knows an
address could keep its owner's link from ever working by asking again, and
could fill the inbox while at it. The cooldown is read from the tokens
themselves, so no email address is ever stored for it. Requests for one
account are serialized by a lock on its row, so requests arriving at the
same moment issue one link between them, not one each. Redeeming a link
takes the same lock, and takes it before the token's own, which is the order
a request for a new link takes them in: the two cannot deadlock.

**Storage.** The counters are rows in the `rate_limits` table (PostgreSQL, no
Redis and nothing in memory), so they are shared by every worker process and
survive a restart. A row holds a key, the start of its window and a count.
The key is an HMAC-SHA256, under `RATE_LIMIT_SECRET`, of which limit it is
and for whom: no address and no identifier is stored, and without the secret
a row cannot be tested against a guess. Counting is a single
`INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING`, which PostgreSQL
serializes per row: it takes a place if one is free and takes nothing if
none is, so a counter never exceeds its limit. It is committed on its own,
on the request's database session, which is why it is only called where that
session holds no other changes. A place is given back only in the window it
was taken in. There is one row per key, reused from window to window. The
row of a key that is not seen again is removed by the
[retention purge](#retention) once its window is over, and those under the
names of an account that is deleted go with it.

**The client's address** is the peer of the connection. Behind a reverse
proxy the server must be started so that it takes the address from the proxy
(uvicorn's `--proxy-headers` with `--forwarded-allow-ips` set to the proxy),
or every client shares one address and its limits. Forwarding headers are
never read from clients directly.

### Requests and responses

A request body larger than `MAX_REQUEST_BODY_BYTES` (64 KiB) is refused with
`413` before it reaches validation or a password hash, whether or not it
declares its length. A declared length is only taken at its word when the
request has no `Transfer-Encoding`; with one, the body is measured as it
arrives. The largest body the API takes is a few kilobytes.

Every response carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`,
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` and,
unless the route set its own, `Cache-Control: no-store`, errors included.

A display name is 1 to 50 characters of any script. Two rules keep it from
showing as something it is not, at registration and in the profile alike
(`422` otherwise):

- It must contain at least one letter, number, punctuation mark or symbol.
  Combining marks, variation selectors and spaces are allowed beside those
  but do not make a name by themselves.
- It must not contain control characters (NUL among them), zero-width and
  other format characters, direction overrides, line separators, private-use
  or unassigned characters, or the blank Hangul and Braille characters. The
  zero-width joiner and non-joiner are the exception, where they join:
  between two non-ASCII characters, as Persian and emoji sequences need.

Which characters are assigned is what the Python in use knows of Unicode, so
a character newer than that is refused until Python is. Look-alike letters of
different scripts are not detected, and display names are not unique.

### Email

There is no email provider yet. `app/services/email.py` defines the
`EmailSender` interface; in development the links are logged, and in production
nothing is sent, so accounts cannot be verified in production until a provider
is added there. For the same reason the [retention purge](#retention) does
not remove unverified accounts unless it is told to.

To verify an account locally, register, then copy the token from the
`[development] Email verification link` line in the server log:

```bash
curl -X POST localhost:8000/auth/verify-email \
     -H 'Content-Type: application/json' -d '{"token": "<token>"}'
```

## User profiles

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /users/me` | authenticated | The caller's own profile, with the fields only they may see |
| `PATCH /users/me` | authenticated | Change the caller's own profile |
| `POST /users/me/export` | authenticated, with the password | Everything of the caller's own, see [Data export](#data-export) |
| `GET /users/{username}` | public | Anyone's public profile |

A profile is the existing `users` row; there is no separate profile table.
`followers_count` and `following_count` are counted from `follows` on every
request and are not stored anywhere.

### Public and own views

The two views are separate schemas in `app/schemas/user.py`, each listing its
fields in full.

- **Public** (`GET /users/{username}`): `id`, `username`, `display_name`, `bio`,
  `avatar_url`, `followers_count`, `following_count`, `following`,
  `created_at`. The query selects only these columns, so the private ones are
  never read on this path. `following` says whether the caller follows the user
  (see [Follows](#follows)); it is the one field that depends on who is asking,
  so the response is sent with `Cache-Control: no-store`.
- **Own** (`GET /users/me`): the public fields except `following`, plus `email`
  and `email_verified_at`.

`password_hash`, `is_active` and `updated_at` are in neither.

The username in the path is matched in its canonical lowercase form. Every
account that is not shown gives the same `404`: one that does not exist, one
that is deactivated, and one whose email address was never verified. Every
other account has a profile, and it is the same kind of profile: there is no
privacy setting, and no field that says anything of the kind.

### Editing

`PATCH /users/me` changes only the fields that are sent:

| Field | Rule | `null` |
| --- | --- | --- |
| `display_name` | 1 to 50 characters after trimming, any script | rejected |
| `bio` | up to 160 characters of plain text after trimming | clears it |
| `avatar_url` | an absolute `http(s)` URL | clears it |

A blank `bio` is stored as no bio. A request with none of these fields is
rejected with `422`. Any other field in the body is ignored, so `username`,
`email`, `is_active` and the rest cannot be changed here; the service also
refuses to write any column outside these three.

The endpoint takes no user identifier. It always edits the authenticated user,
so there is no way to address someone else's profile.

`avatar_url` is checked for its form only. The server never requests it.
Profile text is stored and returned exactly as written, as JSON; escaping it
for display is the client's job.

## Posts

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `POST /posts` | authenticated | Publish a post, or a reply to one |
| `GET /posts/{post_id}` | public | A single post |
| `PATCH /posts/{post_id}` | author | Change the text, for 60 minutes |
| `DELETE /posts/{post_id}` | author | Delete the post, for good |
| `GET /posts/{post_id}/replies` | public | The replies to the post, newest first |
| `POST /posts/{post_id}/like` | authenticated | Like the post |
| `DELETE /posts/{post_id}/like` | authenticated | Take the like back |
| `POST /posts/{post_id}/repost` | authenticated | Repost the post |
| `DELETE /posts/{post_id}/repost` | authenticated | Take the repost back |
| `GET /users/{username}/posts` | public | A user's posts, newest first |

"Public" means no session is needed. Part of the answer still depends on who is
asking (`liked_by_me`, `reposted_by_me`), so these responses are sent with
`Cache-Control: no-store`.

A post is text only. A reply is a post whose `parent_post_id` names another
post; there is no separate reply table or model, and a reply can be replied to
in turn. Everything below applies to replies exactly as it does to posts.

```json
{
  "id": "…",
  "author": {"id": "…", "username": "alice", "display_name": "Alice", "avatar_url": null},
  "content": "Hello Hopsnop!",
  "parent_post_id": null,
  "is_reply": false,
  "created_at": "2026-10-05T12:00:00Z",
  "updated_at": "2026-10-05T12:00:00Z",
  "like_count": 12,
  "liked_by_me": true,
  "repost_count": 4,
  "reposted_by_me": false
}
```

A request can set `content` and, when creating, `parent_post_id`. The author is
always the authenticated user, and the timestamps and the deletion state are
set by the server; any other field in a request body is ignored.

### Content

1 to 300 characters. Surrounding whitespace is trimmed first, so text that is
only whitespace is empty and is rejected. Text that is too long is rejected,
never cut. Characters are Unicode code points, not bytes, which is also how the
`char_length` check constraint on `posts.content` counts them: an emoji made of
several code points counts as several.

Content is stored and returned exactly as written, as JSON; escaping it for
display is the client's job.

### Visibility

Which posts are shown is decided in one place, `_visible_posts` in
`app/services/posts.py`. Every read, and every check that a post exists for
someone, goes through it.

A post is shown if it is not deleted and its author's account is shown at all
(active and verified, the same rule as for profiles). That is the whole rule,
and it is the same for every reader, signed in or not. No account keeps its
posts to itself, and following an account plays no part in reading them (see
[Follows](#follows)). Who is asking decides only `liked_by_me` and
`reposted_by_me`.

The account that decides is always the post's own author. A reply by an account
that is no longer shown is hidden while the post it answers stays, and a reply
stays visible when the post it answers is deleted or its author is deactivated.

A post that is not shown is answered like one that does not exist:
`404 Post not found`, from the same single query. That holds for writes too.
`PATCH` or `DELETE` on a hidden post is a `404`, never a `403`, so a write
attempt cannot confirm that an id belongs to a post. `403` is only given for a
post the caller can read but did not write.

`GET /users/{username}/posts` answers `404 User not found` for the accounts
whose profile is not shown, and the posts of every other account to anyone.

### Editing

Only the author can edit, only `content`, and only while the post is less than
60 minutes old. The deadline is `created_at` plus 60 minutes; it is computed,
not stored, and editing does not move it. At exactly 60 minutes the post is
locked, and `PATCH` answers `409`.

`updated_at` is the time of the last edit and equals `created_at` for a post
that was never edited. Nothing else changes it, including deletion.

### Deletion

`DELETE` erases the post and returns `204`. Nothing of its text and nothing
that says who wrote it is kept, and its likes and reposts are removed. From
then on the post is not found by anyone, its author included: it cannot be
read, edited, liked, replied to, or deleted again (`404`).

What becomes of the row depends on whether anyone has replied:

- **No replies:** the row is removed.
- **Replies:** the row is kept as a **tombstone**. Its `author_id` and its
  `content` are set to NULL and `deleted_at` to the moment of the erasure; it
  keeps its `id`, its `parent_post_id` and its timestamps, which is what its
  replies need to stay where they are in the thread. The replies themselves
  are not touched. They stay ordinary posts, shown everywhere, with the same
  `parent_post_id`.

A tombstone exists only for its replies. When the last of them is deleted,
the tombstone is removed with it, and so is the tombstone above that if it
has no reply left either, and so on up the thread. Removal stops at the first
row that is still a post, or that something else still answers.

A tombstone is never sent to a client. `GET /posts/{id}` and
`GET /posts/{id}/replies` answer `404` for it, exactly as for a post that
never existed, it is in no feed and no list, and a post in a response always
has an author and a text. A reply to an erased post is found through the
feeds, its author's posts and its own id; it cannot be listed through the
post it answered.

Deleting posts the old way left their rows as they were, with `deleted_at`
set. The migration that introduced erasure (`f55b0cf7f446`) applied the rule
above to those rows. It cannot be downgraded while a tombstone exists: the
previous schema requires an author and a text, and what was erased is gone.

#### How it is done

`app/services/erasure.py` holds the one operation both cases are: every post
to be erased becomes a tombstone, and every tombstone without a reply is
removed, repeatedly, upwards. A deletion is one transaction.

Two things keep it correct when other requests arrive at the same moment:

- A request that writes a row referring to a post (a reply to it, a like, a
  repost) first locks the post `FOR KEY SHARE` and keeps it until its own row
  is committed. A deletion locks the post `FOR UPDATE`. One of the two comes
  first. If it is the reply or the like, the deletion waits and then deals
  with it: the like is removed, the reply keeps the post as a tombstone. If
  it is the deletion, the other waits and then gets the ordinary `404`.
  Readers take no locks and never wait.
- Every erasure first takes one advisory lock (`pg_advisory_xact_lock`), so
  erasures happen one at a time. Removing emptied tombstones can reach into
  threads that another erasure is working on, and two of them could
  otherwise lock such rows in opposite orders. The lock is taken before any
  post is locked; nothing but another erasure waits for it.

The account that replies or likes is held in the same way, and before the
post: its own row, `FOR KEY SHARE`, so that it cannot be removed while
something is being written in its name (see
[deleting an account](#deleting-an-account)). Should a write ever lose what
it refers to all the same, the failed foreign key is answered with `409`, not
`500`.

Measured on 300,000 posts and 200,000 likes:

| Operation | Time |
| --- | --- |
| Delete a post without replies (through the API) | 10 ms median |
| Delete a post with 100 replies and 50 likes | 6 ms median |
| Delete a post with 10,000 replies and 2,000 likes | 14 ms |
| Delete the last reply under 10 / 100 / 1,000 tombstones | 39 ms / 0.27 s / 2.6 s |
| Erase 1,000 / 10,000 / 100,000 posts of one author at once | 50 ms / 0.36 s / 3.4 s |
| A deletion while another erasure holds the lock for 1 s | 1.03 s |
| Others reading, posting, replying, liking, following meanwhile | unchanged (5 to 10 ms) |

The cost of a deletion does not depend on how many replies or likes the post
has, only on how many tombstones it empties above it (two statements each).
A deletion waits for an erasure that is under way for as long as that takes
and no longer. No index was added: finding a post's replies uses
`ix_posts_parent_post_id_created_at`, and its likes and reposts
`ix_likes_post_id` and `ix_reposts_post_id`.

### Replies

`parent_post_id` must name a post the author can currently see. A parent that
does not exist, was deleted, or is hidden gives the same `404 Parent post not
found`. The parent of a post cannot be changed afterwards.

`GET /posts/{post_id}/replies` lists the replies to a post, newest first, as
the same page of the same post objects as every other list of posts (see
[Pagination](#pagination)). It needs no session, and whom the caller follows
plays no part: Alice posts, Bob replies, and Carol, who follows neither, reads
Bob's reply there like anyone else.

- Only direct replies are listed. A reply to a reply is in that reply's own
  list; nothing is flattened or nested.
- The post asked about must be shown. One that does not exist, was deleted, or
  whose author's account is not shown gives `404 Post not found`, the answer
  and the single query that reading it gives, and its replies are never read.
  Each of them is still a post of its own, found by its id, in its author's
  list and in the feeds.
- Each reply is shown or not by its own author, like any post. Replies that
  are not shown are left out by the query, before the page is cut.

A page is two statements: one that finds the post, and one for the replies with
their authors and counts, however many there are. The second is served by the
existing `ix_posts_parent_post_id_created_at`, walked backwards from the
cursor's position. No index was added: measured on a post with 50,000 replies
among 670,000 posts, the first page and a page 150 days deep each took about
0.2 ms.

### Likes and reposts

`POST` makes the caller's like or repost of a post, `DELETE` takes it back.
All four need a session of an active account with a verified email address
(`401` without a session, `403` for an unverified address), and each answers
`200` with where the post now stands with the caller:

```json
{"liked": true, "like_count": 12}
{"reposted": false, "repost_count": 4}
```

A request can be repeated. Liking a post that is already liked leaves it liked,
and taking back a like that is not there leaves it not there; both are answered
with the current state, never with `409`. The same goes for reposts, and a like
and a repost of the same post are independent of each other.

Only a post that is shown can be liked or reposted, replies included, and
one's own posts like anyone else's. Any other post, whether hidden, deleted or
nonexistent, is `404 Post not found`, under the same rules as reading it, so an
attempt reveals nothing about a hidden post. That also holds for taking back.
The likes and reposts of a post whose author is no longer shown stay in the
database with it, are reported to nobody, and are shown again if the post
becomes readable again. Those of a post that is deleted are removed with it
(see [Deletion](#deletion)); while a like or repost is being written, the
post is held in place, so it cannot be left on a post that was just erased.

Every post in every response carries `like_count`, `liked_by_me`,
`repost_count` and `reposted_by_me`. The counts are the same for every reader.
The other two are the reader's own and are `false` for an anonymous request.
Who else liked or reposted a post is not exposed anywhere: there is no endpoint
that lists it, and only numbers leave the `likes` and `reposts` tables.

The counts are not stored. They are counted from the rows by the statement that
loads the posts (through `ix_likes_post_id` and `ix_reposts_post_id`), and the
reader's own are looked up by primary key in that same statement, so a page
costs one statement however many posts, likes and reposts are on it. No counter
can drift from the rows; the price is that a count costs as much as the post
has likes.

A second like by the same user is ruled out by the primary key
`(user_id, post_id)`. Requests insert with `ON CONFLICT DO NOTHING` instead of
checking first, so two identical requests arriving at once cannot both insert
and neither fails.

Likes and reposts change nothing about which posts a list contains or in which
order. A repost is, for now, a mark on the post and a number; it does not put
the post anywhere.

### Pagination

Lists are paginated with a cursor, not with an offset or page number:

```
GET /users/alice/posts?limit=20
{"items": [...], "next_cursor": "AAZc…"}

GET /users/alice/posts?limit=20&cursor=AAZc…
{"items": [...], "next_cursor": null}
```

`limit` is 1 to 50 and defaults to 20; anything else is a `422`. `next_cursor`
is `null` on the last page.

Rows are ordered by `created_at` descending, then `id` descending, so the order
is total even when timestamps are equal. The cursor encodes those two values of
the last row of the page, and the next page is the rows that sort after them
(`WHERE (created_at, id) < (…, …)`). Because a page is addressed by a position
and not by a count of rows to skip, posts that are created or deleted between
two requests cause neither duplicates nor gaps.

The cursor is opaque but not secret: it holds nothing the page did not already
show, and it only selects a position. What a query returns is decided by the
query's own conditions on every page, so a cursor, genuine or made up, gives no
access to anything. A value that is not a cursor is a `400`.

The implementation is `app/core/pagination.py` (`paginate`) and the
`Pagination` dependency in `app/api/deps.py`; neither is specific to posts.

### Errors

| Status | When |
| --- | --- |
| `400` | Malformed `cursor` |
| `401` | No usable session, on an endpoint that needs one; the account was deleted while the request was under way |
| `403` | Not the author of the post; unverified email; cross-site request |
| `404` | Post, parent post or user not found, or not visible to the caller |
| `409` | The 60-minute edit window has passed; a write that referred to something removed at that moment |
| `422` | Invalid `content`, `parent_post_id`, `post_id` or `limit` |

## Feed

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /feed` | public | For You: every post that is shown, newest first |
| `GET /feed/following` | authenticated | Following: the posts of the users the caller follows |

For You is, for now, deliberately not a recommendation. It is every post that
is shown, in chronological order, newest first. Nothing is ranked, and likes,
reposts and follows play no part in it.

```
GET /feed?limit=20
{"items": [...], "next_cursor": "AAZc…"}

GET /feed?limit=20&cursor=AAZc…
{"items": [...], "next_cursor": null}
```

The answer is the same page of the same post objects as
`GET /users/{username}/posts`, with the same cursor, the same limits and the
same errors (see [Pagination](#pagination)). It is sent with
`Cache-Control: no-store`.

### What is in it

A post is in the feed if it is shown under the
[visibility rules](#visibility): not deleted, author active and verified. The
feed has no rule of its own; `list_for_you_feed` is `_visible_posts`, paginated.

- No session is needed, and the same posts are in the feed with or without
  one. An author finds their own posts in it like anyone else's.
- Whom the caller follows changes nothing. That is the
  [Following feed](#following).
- Replies are posts. Each appears at its own place in time with its
  `parent_post_id`; nothing is grouped into conversations. A reply stays in the
  feed when the post it answers is deleted or its author is deactivated, and
  only the parent's id is shown, as on every other endpoint.

Posts that are not shown are left out by the query itself, before the page is
cut. They take up no room on a page, and nothing in a response, `next_cursor`
included, says that they exist.

### Query

One statement returns a page together with its authors, however many posts and
authors there are: `posts` joined to `users`, filtered, ordered and limited to
`limit + 1` rows by the database.

It is served by the existing `ix_posts_created_at` index. PostgreSQL walks it
backwards from the cursor's position and stops when the page is full, so a deep
page costs the same as the first one. No index was added for the feed: a
composite `(created_at, id)` index was measured on 300,000 posts and changed
nothing, because rows that share a timestamp to the microsecond are rare. What
the scan cannot skip cheaply is a long run of consecutive posts by accounts
that are not shown, since that is decided in `users`; in the same measurement
5,000 such posts in a row cost under 2 ms.

### Following

`GET /feed/following` is the For You query with one more condition: the
caller follows the post's author. It answers with the same page of the same
post objects, the same cursor, the same limits and `Cache-Control: no-store`.

It needs a session of an active account with a verified email address. Without
one the answer is `401`, also for a cookie that is stale, expired or revoked
and for a deactivated account; For You answers those as anonymous requests,
this feed does not. A session of an unverified account gets `403`.

Whose feed it is, is always the signed-in user. The endpoint takes `limit` and
`cursor` and nothing else, so there is no way to name another user, and any
other parameter is ignored.

- The condition is `is_followed_by`, the same `EXISTS` on `follows` that
  profiles and stories use, evaluated by the query on every request. Nothing
  is stored per reader. Unfollowing takes all of an account's posts out at
  once, old ones included, and following again brings back those that are
  shown then.
- A user's own posts are not in it: nobody follows themselves.
- Replies are in it as in For You, each by its own author. A followed
  account's reply is there whoever wrote the post it answers, and a reply by
  someone who is not followed is not there even under a followed account's
  post. A reply stays when the post it answers is deleted; of that post only
  the id is shown.
- A repost puts nothing in it. Reposts are counted on the post and that is
  all.
- Following nobody, or only accounts without posts, is an empty page with
  `200`.

A cursor is a position in time here too, and the same one For You uses. One
taken from another list, or made up, selects a position among the posts the
caller's own follows allow and nothing else.

A page is one statement, after the one that finds the session: `posts` joined
to `users` with the `follows` condition, filtered, ordered and limited by the
database. No index was added. Measured on 20,000 accounts, 670,000 posts and
405,000 follows, PostgreSQL chooses between two plans from the existing
indexes:

| Viewer follows | First page | Deep page | Plan |
| --- | --- | --- | --- |
| nobody | 0.05 ms | | `pk_follows`, then nothing |
| 5 accounts | 0.8 ms | 0.2 ms | `pk_follows`, then `ix_posts_author_id_created_at` per author, top-N sort |
| 3 accounts whose posts are older than all others | 0.2 ms | | the same |
| 1 account with 20,000 posts | 9 to 24 ms | 2 to 4 ms | the same |
| 200 accounts | 10 to 14 ms | 10 ms | `ix_posts_created_at` backwards, probing `follows` per author |
| 5,000 accounts | 0.4 ms | | the same |

The slowest case is the one with few followed accounts that have very many
posts, since all of an author's posts from the cursor on are read before the
newest are kept. An index on `(author_id, created_at DESC, id DESC)` was
tried and was not used by the planner, so it was not added.

## Follows

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `POST /users/{username}/follow` | authenticated | Follow the user |
| `DELETE /users/{username}/follow` | authenticated | Unfollow the user |
| `GET /users/{username}/follow-status` | public | Whether the caller follows the user |
| `GET /users/{username}/followers` | public | The users who follow the user |
| `GET /users/{username}/following` | public | The users the user follows |

A follow is one row in `follows`: this account follows that one. It takes
effect at once. There are no follow requests, no approval and no state in
between, and following goes one way: it says nothing about being followed
back.

Hopsnop has no public and private accounts. There is no privacy setting, so
every account that is shown can be followed by any other, and anyone can read
its lists. Following does not change which posts can be read, nor what is in
the For You feed or in a post's replies. It decides what is in the
[Following feed](#following) and whose [stories](#stories) are shown.

### Following and unfollowing

Both need a session of an active account with a verified email address (`401`
without a session, `403` for an unverified address), and both answer `200` with
the resulting state:

```json
{"following": true}
```

Who follows is always the signed-in user. The requests take no body, and
nothing in a request can name another follower.

A request can be repeated. Following a user who is already followed leaves
them followed, and unfollowing a user who is not followed leaves them not
followed; both are answered with the current state, never with `409`.

Following yourself is `400 You cannot follow yourself`. The service refuses it
before the database is asked; the check constraint `ck_follows_no_self_follow`
remains as the last line. Unfollowing yourself is simply `{"following": false}`.

Only an account that is shown can be followed: active, with a verified email
address, the same rule as for profiles. Any other name is `404 User not found`,
whatever the reason, for every endpoint here. A follow of an account that later
stops being shown stays in the table, is listed for nobody, and can be ended
once the account is shown again.

### Follow state and counts

`GET /users/{username}/follow-status` answers `{"following": true}` or
`{"following": false}`. It needs no session; an anonymous caller follows
nobody, so the answer is then `false`. The same value is the `following` field
of the user's public profile.

It is always the caller's own follow. Who else follows a user is only in the
lists below.

`followers_count` and `following_count` on a profile are counted from the rows
by the statement that reads the profile, together with `following`. They are
row counts: a follow by an account that is no longer shown is still counted,
although that account is on no list.

### Lists

```
GET /users/alice/followers?limit=20
{
  "items": [{"username": "bob", "display_name": "Bob", "avatar_url": null}],
  "next_cursor": null
}
```

`followers` and `following` need no session and are the same for every reader.
An item holds what a list shows and what links to the profile: `username`,
`display_name`, `avatar_url`. There is no id in it.

Only accounts that are shown are listed. They are left out by the query, before
the page is cut, so a list reveals no account that `GET /users/{username}`
hides, and `next_cursor` does not either.

The most recent follow comes first. A follow has no id of its own, so follows
made at the same instant are ordered by the id of the listed account, which is
unique within one list. Paging is the cursor pagination of the other lists
(see [Pagination](#pagination)): the same `limit`, the same cursor format, the
same `400` for a value that is not a cursor. `paginate` takes the name of the
tie-breaking column for this.

The cursor of these lists holds the time of the last follow on the page and the
id of that account. The id is public through the profile; the time is not shown
anywhere else, so each page gives away when its last follow was made.

A page is one statement for the follows and the users on it, after one that
finds the account.

### Indexes

No index was added. The primary key `(follower_id, following_id)` answers "does
A follow B", "whom does A follow" and the following count; the existing
`ix_follows_following_id_follower_id` answers "who follows B" and the follower
count.

Neither is ordered by time, so a page of a list sorts all of that account's
follows first. Measured on 4.1 million follows, that is under half a
millisecond for an account with 20 followers and 15 to 19 ms for one with
100,000, the same as counting them for its profile. An index on
`(following_id, created_at DESC, follower_id DESC)`, and its twin for the other
direction, brings the page to under a millisecond and is the change to make if
accounts of that size appear.

### Concurrency

A second follow of the same account by the same user is ruled out by the
primary key. Requests insert with `ON CONFLICT DO NOTHING` instead of checking
first, so two identical requests arriving at once cannot both insert and
neither fails.

Both accounts are held in place while the row is written (`FOR KEY SHARE`,
the follower's own first). An account that is being deleted at that moment is
either waited for and then not found (`404`), or waits itself until the
follow is written and then takes it along.

## Stories

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `POST /stories` | authenticated | Publish a story |
| `GET /stories` | authenticated | Active stories of the caller and of the users they follow |
| `GET /stories/{story_id}` | authenticated | A single active story |
| `DELETE /stories/{story_id}` | author | Delete the story |
| `POST /stories/{story_id}/view` | authenticated | Record that the caller viewed the story |

A story is an image, by its URL, with an optional caption, shown for 24 hours.
Every endpoint needs a session of an active account with a verified email
address (`401` without a session, `403` for an unverified address). Nobody reads
a story anonymously, and every response is sent with `Cache-Control: no-store`.

```json
{
  "id": "…",
  "author": {"username": "alice", "display_name": "Alice", "avatar_url": null},
  "media_url": "https://media.example.com/stories/1.jpg",
  "media_type": "image",
  "caption": "Good morning",
  "created_at": "2026-10-06T12:00:00Z",
  "expires_at": "2026-10-07T12:00:00Z",
  "viewed_by_me": false,
  "view_count": null
}
```

### Publishing

A request can set `media_url` and `caption`; any other field in the body is
ignored. The author is always the authenticated user, `media_type` is always
`image`, and both timestamps are set by the server.

| Field | Rule |
| --- | --- |
| `media_url` | required, an absolute `http(s)` URL |
| `caption` | optional, up to 150 characters of plain text after trimming; blank or `null` is no caption |

`media_url` is checked for its form only. The server never requests it, and
there are no uploads. A story cannot be edited.

### Who may see a story

Who may see a story is decided in one place, `_shown_to` in
`app/services/stories.py`. Every read, view and deletion goes through it. A
story is shown to the caller if all of this holds:

- it has not expired;
- its author's account is shown (active and verified, the same rule as for
  profiles);
- the caller is the author, or follows the author at this moment.

The last condition is the `follows` table and the same check that answers
`follow-status` (see [Follows](#follows)). It is evaluated by every query and
nothing is kept per reader, so unfollowing ends access at once and following
again brings the stories that are still active back. Following goes one way:
being followed by someone shows nothing of theirs. There are no other rules
about who may see a story.

Any other story is answered like one that does not exist: `404 Story not
found`, from the same single query, whether it is expired, deleted, by an
account the caller does not follow, or by one that is not shown. That holds for
viewing and deleting too.

### Expiration

`expires_at` is `created_at` plus 24 hours. It is stored with the story when it
is created and never moved. A story stops being shown at that moment itself,
also to its author. An expired story stays in the table, where only an
[export](#data-export) of its author's data still finds it, until the
[retention purge](#retention) removes it.

### The list

`GET /stories` is the caller's own active stories and those of the users they
follow, in one flat list, newest first. Stories the caller has viewed stay in
it where they were; `viewed_by_me` tells them apart. Nothing is grouped by
author. Paging is the cursor pagination of the other lists (see
[Pagination](#pagination)): the same `limit`, the same cursor, the same `400`
for a value that is not a cursor.

Reading the list or a single story records nothing. Like every `GET` in the
API, both are free of side effects.

### Views

`POST /stories/{story_id}/view` records that the caller has viewed a story they
can see and answers `200` with the resulting state:

```json
{"viewed": true}
```

A user views a story once. The request can be repeated and is answered the same
way each time, never with `409`; the primary key `(story_id, viewer_id)` rules
out a second row, and requests insert with `ON CONFLICT DO NOTHING` instead of
checking first, so two identical requests arriving at once cannot both insert
and neither fails. Whose view it is, is always the signed-in user.

An author's look at their own story is not a view: nothing is recorded and the
answer is `{"viewed": false}`.

`view_count` is told to the author alone. For every other reader it is `null`,
and the query itself returns `NULL` for it, so the number does not leave the
database. Who viewed a story is told to nobody: there is no endpoint that lists
viewers. A view stays recorded, and counted, when the viewer later unfollows
the author.

### Deletion

`DELETE` removes the row and returns `204`; the story's views are removed with
it (`ON DELETE CASCADE`). Only the author
can delete: a follower, who can read the story, gets `403`, and anyone who
cannot see it gets `404`.

### Query and indexes

One statement returns a page together with its authors, the caller's own
views and, for the caller's own stories, the view counts. No schema change and
no index was added: `ix_stories_author_id_expires_at` serves the active stories
of an author, the primary key of `follows` the follow check, the primary key of
`story_views` the count, and `ix_story_views_viewer_id_story_id` the caller's
own views.

## Data export

`POST /users/me/export` returns everything Hopsnop holds that is the signed-in
user's own, as one JSON document to download.

| | |
| --- | --- |
| Body | `{"password": "..."}`, the current password. Nothing in the request says whose data it is: it is the session's |
| Needs | A session. A verified email address is not required |
| `200` | The document, with `Content-Disposition: attachment; filename="hopsnop-export-<username>-<date>.json"` and `Cache-Control: no-store` |
| `400` | Wrong password (`Current password is incorrect.`), or the password was changed or reset while the request was under way |
| `401` | No session, or the account was removed while the request was under way |
| `403` | Sent from another site |
| `422` | No usable password in the body |
| `429` | Too many wrong passwords for this account, or too many exports of it, see below |

It is a `POST` because it takes a password, which belongs in a body and not
in a URL, and because a link alone must not be enough to fetch an account's
data.

### The document

```json
{
  "export_version": 1,
  "generated_at": "2026-10-09T12:00:00.123456Z",
  "account": {
    "id": "0d0e6b0c-...", "username": "alice", "email": "alice@example.com",
    "display_name": "Alice", "bio": null, "avatar_url": null,
    "is_active": true, "email_verified_at": "2026-03-01T08:00:00.000000Z",
    "created_at": "2026-03-01T07:55:10.482113Z",
    "updated_at": "2026-03-01T08:00:00.000000Z"
  },
  "sessions": [
    {"created_at": "...", "last_used_at": "...", "expires_at": "...",
     "revoked_at": null, "is_current": true}
  ],
  "account_requests": {
    "email_verifications": [{"requested_at": "...", "expires_at": "...", "used_at": "..."}],
    "password_resets": [{"requested_at": "...", "expires_at": "...", "used_at": null}]
  },
  "posts": [
    {"id": "...", "content": "...", "parent_post_id": null,
     "created_at": "...", "updated_at": "..."}
  ],
  "likes": [{"post_id": "...", "created_at": "..."}],
  "reposts": [{"post_id": "...", "created_at": "..."}],
  "following": [{"username": "bob", "created_at": "..."}],
  "followers": [{"username": "carol", "created_at": "..."}],
  "stories": [
    {"id": "...", "media_url": "...", "media_type": "image", "caption": null,
     "created_at": "...", "expires_at": "...", "view_count": 3}
  ],
  "story_views": [{"story_id": "...", "viewed_at": "..."}]
}
```

| Part | Holds |
| --- | --- |
| `account` | The account's row, without the password hash |
| `sessions` | Every session still stored, ended ones included, by its times. `is_current` marks the one that asked |
| `account_requests` | The verification and reset links emailed for the account, by when each was asked for, expired and was used |
| `posts` | The posts and replies the account wrote. A reply has a `parent_post_id` |
| `likes`, `reposts` | The ones the account made: of which post, and when |
| `following`, `followers` | The accounts on the other side of its follows, by username, and since when |
| `stories` | The stories still stored, expired ones included, each with how many views it has |
| `story_views` | The stories of others that the account viewed, and when |

The schemas are in `app/schemas/export.py`, one per part, each listing its
fields in full; the same are served in the OpenAPI schema in development.

- **Every part is always there.** A list with nothing in it is `[]`, a value
  that is not set is `null`; no field is ever left out.
- **Order.** The fields come in the order above. Every list is in the order
  its entries came about, oldest first. Entries of the same moment are
  ordered by a key of their row, so the same data always gives the same
  document, byte for byte, apart from `generated_at`.
- **Moments** are written one way throughout: ISO 8601 (RFC 3339) in UTC, to
  the microsecond, ending in `Z`, always 27 characters, as in
  `2026-03-01T08:11:00.000000Z`.
- **Version.** `export_version` is `1`. A change that could break a reader of
  these documents would be a new version.

### What is in it and what is not

Every column of the database is either exported or left out for a stated
reason. The list is kept as a test (`MATRIX` in `tests/test_data_export.py`),
which fails for a column that is added and not yet decided on.

| Table | Exported | Left out, and why |
| --- | --- | --- |
| `users` | everything else | `password_hash`: a credential |
| `sessions` | the four timestamps, and which one is current | `id`: addresses a session on the server and says nothing about the user. `token_hash`: a credential. `user_id`: the account itself |
| `email_verification_tokens`, `password_reset_tokens` | when asked for, when expiring, when used | `id`: internal. `token_hash`: a credential. `user_id`: the account itself |
| `posts` | `id`, `content`, `parent_post_id`, both timestamps | `author_id`: the account itself. `deleted_at`: only set on a tombstone, which has no author and is in nobody's export |
| `likes`, `reposts` | `post_id`, `created_at` | `user_id`: the account itself |
| `follows` | the other account's username, `created_at` | the ids of other accounts |
| `stories` | everything else | `author_id`: the account itself |
| `story_views` | `story_id` and `viewed_at` of views the account made; the number of views of each of its stories | who viewed its stories: that is the viewers' data, and is told to nobody |
| `rate_limits` | nothing | counters under keyed hashes, attributable to no account without the server's secret; a control, not a record of the user |

**Never in an export:** the password or its hash, session tokens or their
hashes, session ids, the hashes or ids of emailed links, the keys of
rate-limit counters, `RATE_LIMIT_SECRET`, or any other secret. None of these
is read by any statement of the export, and none has a field in the schema.
The tests search the whole document for every such value in the database,
whoever's it is.

**Other people's data** is in an export only as far as the user could see
it anyway: the id of a post they answered, liked or reposted, the id of a
story they viewed, and the username of an account they follow or are
followed by. An account that is shown to nobody (deactivated, or never
verified) is not named, as in the lists of followers.

**Deleted content.** A post that was [deleted](#deletion) is erased, and
what may be left of it is a tombstone with no author. It is nobody's and is
in nobody's export, its former author's included; nothing is reconstructed.
A reply the user wrote to a post that has since been deleted is exported
with the `parent_post_id` it has, and nothing else about that post.

### What kind of picture it is

**An export is not a snapshot of one moment.** It represents the user's data
as observed by the individual export queries during the export operation.
The parts are read one after the other, each by one statement, and each sees
what was committed when it ran (READ COMMITTED). What the user does while an
export is being made can therefore be in a later part and not in an earlier
one. Each part by itself is whole and in order. There is no snapshot
machinery.

What does stand still is the account. Once the password is confirmed, the
account's row is locked (`FOR NO KEY UPDATE`, the lock a password change
takes) until the document is complete. The account cannot be removed and its
password cannot be replaced meanwhile, so an export is either made in full,
of the account that asked and with the password that account has, or it is
not made:

| The other request | If it comes first | If the export comes first |
| --- | --- | --- |
| Account deletion | Export `401`, no data | The deletion waits; the export is complete |
| Password change or reset | Export `400`: the password it was given is no longer the account's | The change waits; the export is complete |
| Login to the account | | The login waits for the export |
| Another export of the account | They take turns | |
| The user's posts, likes, follows, deletions | Not held up; in the parts read after them | |

### Limits

Two counters, both per account and both within the window of
[rate limiting](#rate-limiting):

- wrong passwords, shared with everything else that asks for the password
  again (`PASSWORD_CONFIRMATION_FAILURES`, 5);
- exports (`DATA_EXPORTS_PER_ACCOUNT`, 3), because an export reads everything
  an account has.

A request takes a place in both or in neither. A wrong password is not
counted as an export, and an export that is refused for the limit is not
counted as a wrong password, so neither limit can be used to exhaust the
other.

### How it is made

`app/services/export.py`: after the lock, ten statements read the ten parts,
each naming the columns it reads and each ordered by the database. The number
of statements does not depend on how much the account holds (17 for the whole
request, the session lookup and the counters included); there is no query per
post, per story or per follower. The views of the stories are counted inside
the statement that reads the stories.

Measured on 20,000 accounts, 300,000 posts and 200,000 likes:

| Account | Document | Through the API | Account locked for |
| --- | --- | --- | --- |
| Nothing but one session | 0.8 KiB | 55 ms median | 8 ms |
| 200 posts, 500 likes, 50 reposts, 300 follows, 5 stories | 119 KiB | 66 ms median | 14 ms |
| 100,000 posts, 20,000 likes, 15,000 follows, 100 stories | 26 MiB | 1.6 s median | 1.2 s |

Checking the password is 33 ms of each. For the large account the database
takes about 0.1 s (the posts are found through
`ix_posts_author_id_created_at` and sorted, 75 ms); the rest is building the
document in the application. It is built in memory, about 150 MiB at its
peak for the large account. That is acceptable for the number of exports an
account may ask for; an account very much larger than this would call for
writing the document out in pieces. No index was added.

## Retention

Much of what is stored stops being used without being removed: a story
expires, a session ends, a link is opened or runs out, a rate-limit window is
over, somebody registers and never opens the link. The retention purge
removes such rows once they have been of no use for a configured time. It is
a command, run by hand or by cron. The application never runs it, and there
is no worker, no queue and nothing else to operate.

```bash
python -m app.purge    # dry run: counts, removes nothing
python -m app.purge --execute --database hopsnop --host db.internal:5432    # removes
```

**Run as it is, it removes nothing.** It prints what it would remove and
ends. To remove, it must be given `--execute` and, with it, which database
is meant and where:

- `--database NAME`, compared with the database the server says the
  connection has reached;
- `--host HOST:PORT`, compared with the host and port the connection was
  made to (those of `DATABASE_URL`, with `5432` if it names no port; letter
  case does not matter).

If either differs the command refuses, changes nothing and ends with `2`,
saying where it is and what was asked for. `--execute` without both is
refused as well. A `DATABASE_URL` that points somewhere other than intended
is thus found out before anything is deleted. The name alone would not be
enough for that: the development database of this project and a production
one are both likely to be called `hopsnop`, on different servers. A dry run
ends by printing the two options as they would have to be given for the
database it looked at.

Nothing of the connection but the host, the port and the database name is
ever printed or compared: no user, no password, no URL.

Every run begins by printing what it goes by: the database and host it is
connected to (never the user or the password), the environment, the
rate-limit window, each period, whether unverified accounts are removed, and
the batch size.

```
Retention purge at 2026-10-13T03:15:00+00:00
  database                      hopsnop on db.internal:5432
  environment                   production
  rate-limit window             15 minutes
  expired stories kept          24 hours
  ended sessions kept           30 days
  spent tokens kept             7 days
  finished counters kept        1 hours
  unverified accounts           not removed (PURGE_UNVERIFIED_ACCOUNTS is off)
  batch size                    1000
Removed:
  unverified accounts                  0
  expired stories                    418
  ended sessions                      57
  email verification tokens           31
  password reset tokens                6
  rate limit counters               1204
  total                             1716
```

A dry run prints `Would remove:` in place of `Removed:`, with the rows that
match at that moment. It reads counts and takes no lock. A run that removes
can remove fewer: a row that somebody holds just then is left for the next
run.

The counts are of rows of that kind. What goes with a row is not counted:
the views of a story, the tokens of an account.

| Exit status | Meaning |
| --- | --- |
| `0` | Finished, also when there was nothing to remove, and for a dry run |
| `1` | Failed. What was removed before that stays removed and is printed (see below) |
| `2` | Refused to begin: another database or server than the one named, or `--execute` without `--database` and `--host` |

### Its environment

The command reads its settings exactly as the application does
(`app/core/config.py`): from the process environment, and from `.env` in
the directory it is started in. It must be given the same ones. How, depends
on how the application gets them:

- **From `.env` in the project directory.** Start the command in that
  directory, as below, and it reads the same file. The file holds the
  database password and `RATE_LIMIT_SECRET`: it should be readable by the
  service's user only (`chmod 600 .env`) and is never committed.
- **From an environment file of the service** (systemd's `EnvironmentFile=`,
  a Compose `env_file`, and the like). Have cron read that same file, not a
  copy of it, and run the command as the service's user:

  ```cron
  15 3 * * *  cd /srv/hopsnop-backend && set -a && . /etc/hopsnop/backend.env && set +a && .venv/bin/python -m app.purge --execute --database hopsnop --host db.internal:5432 >> /var/log/hopsnop-purge.log 2>&1
  ```

  with `/etc/hopsnop/backend.env` owned by that user and mode `600`.
- **From the platform** (container or orchestrator environment). Run the
  command as a scheduled job of the same image with the same environment
  and secrets attached.

No setting is written into the crontab itself, where it would be a second
copy to keep in step, and readable by more people.

From cron, once a night, with `.env` in the project directory:

```cron
15 3 * * *  cd /srv/hopsnop-backend && .venv/bin/python -m app.purge --execute --database hopsnop --host db.internal:5432 >> /var/log/hopsnop-purge.log 2>&1
```

What has to be the same, and what happens if it is not:

| Setting | For the purge | If cron does not have the application's value |
| --- | --- | --- |
| `DATABASE_URL` | Which database is purged | Required: without it the command does not start. Pointing at another database or server is caught by `--database` and `--host` |
| `ENVIRONMENT`, and in production `FRONTEND_URL` and `RATE_LIMIT_SECRET` | Not used by the purge, but the settings are checked as a whole | The default is `production`, which does not start without the other two. A cron that was given no environment at all therefore fails, with status `1` and an error in its log, and removes nothing. It does not run on defaults |
| `RATE_LIMIT_WINDOW_MINUTES` | When a counter's window is over | **Must match.** There is no way for the command to know the application's value. With a shorter one, counters are removed while they still count, once the purge's window plus `RATE_LIMIT_RETENTION_HOURS` is less than the application's window: failed logins are then forgotten early. The default retention of an hour covers a difference of up to an hour |
| The retention periods and `PURGE_BATCH_SIZE` | Read by the purge only | Nothing has to match: the application does not use them. If cron lacks them the defaults below apply, which keep rows for longer than zero and remove no account |
| `PURGE_UNVERIFIED_ACCOUNTS` | Read by the purge only | Off unless set. Lacking it can only mean that fewer rows are removed |
| Everything else | Not used | No effect |

Since a wrong value here cannot be detected, it is made visible: every run
prints the window and the periods it used, in the log beside its counts. Run
the command once by hand, without `--execute`, in the way cron will run it,
and read those lines before scheduling it.

### What is removed

Each period begins when the row stops being of use, not when it was written.
A row is removed when that moment is the period ago or longer.

| Rows | Of no use from | Kept for | Setting | Default |
| --- | --- | --- | --- | --- |
| Stories, and who viewed them | `expires_at` | hours | `EXPIRED_STORY_RETENTION_HOURS` | `24` |
| Sessions | `expires_at` or `revoked_at`, whichever is earlier | days | `ENDED_SESSION_RETENTION_DAYS` | `30` |
| Verification and reset tokens | `expires_at` or `used_at`, whichever is earlier | days | `SPENT_TOKEN_RETENTION_DAYS` | `7` |
| Rate-limit counters | the end of their window, `window_start` + `RATE_LIMIT_WINDOW_MINUTES` | hours | `RATE_LIMIT_RETENTION_HOURS` | `1` |
| Accounts that were never verified, **only if `PURGE_UNVERIFIED_ACCOUNTS` is `true`** | `created_at`, see below | days | `UNVERIFIED_ACCOUNT_RETENTION_DAYS` | `7`, and off |

`PURGE_BATCH_SIZE` (`1000`, at most `10000`) is how many rows are removed in
one transaction. The first four periods can be `0`, which removes a row at
the first run after it ends; the period of an account is at least one day.
A value that is negative or out of range keeps the application, and the
command, from starting.

The defaults are engineering choices for development, and whoever operates a
deployment is expected to set their own. No period here is one that a law
prescribes.

Until its row is removed, an expired story, an ended session or a spent
token is still in an [export](#data-export) of the account's data. That is
all these rows are kept for, and why the periods are not simply zero. A
finished counter is not read by anything; removing it changes nothing a
client can observe, because the next request under that key would have
started the counter again from one.

**Nothing else is removed.** A post is never removed for its age. A
tombstone goes when its last reply does, as before, and not sooner. An
account that was ever verified is never removed by the purge, whether or not
anyone has signed in to it since, and whether or not it was deactivated.

**Accounts that were never verified** are not removed unless that is
switched on:

```
PURGE_UNVERIFIED_ACCOUNTS=true
UNVERIFIED_ACCOUNT_RETENTION_DAYS=7
```

It is off by default because the rule presumes that a verification link
reaches whoever registered, and **in production no verification email is
sent yet** (see [Email](#email)). Nobody can verify an account there. With
this switched on, every registration made in production would be removed
once it is `UNVERIFIED_ACCOUNT_RETENTION_DAYS` old, the ones made in good
faith included, and an account that an operator meant to verify by hand
would be gone before they got to it. Switch it on only where verification
emails are delivered, and after a dry run has shown how many accounts it
would take. Setting the period alone does not switch it on. The other four
kinds of rows are removed either way.

Such an account cannot be signed in to, so nothing was ever written in its
name: it holds a username and an email address, possibly somebody else's,
and keeps both from being registered. Switched on, the purge removes it when
all of this is true:

- its address was never verified and it was registered
  `UNVERIFIED_ACCOUNT_RETENTION_DAYS` ago or longer;
- it is active. An account that was deactivated was deactivated by somebody,
  and the purge does not set its names free again;
- no verification link of it can still be opened. Asking for a new link
  keeps an account for as long as that link is valid;
- it has no post, no story and no session. It cannot have any; one that does
  is left alone.

It is removed the way its user would remove it, by the same code as
[deleting an account](#deleting-an-account), and its username and address
can be registered again from that moment. A link that was sent to it opens
nothing afterwards (`400`). Counters of failed logins under its names are
not looked for; they are gone when their window is.

### Running it

**Repeatedly.** What a run removes is read from the rows and decided against
the time at which the run started. A second run finds nothing that the first
should have left, and a run with nothing to remove removes nothing and ends
with `0`. It can be run at any time and as often as wanted.

**In batches.** Rows are removed at most `PURGE_BATCH_SIZE` at a time, each
batch in a transaction of its own, committed before the next begins.
Accounts are removed one in a transaction. No lock is held for longer than
one batch takes, however much there is to remove.

**Failing.** If a run fails (the database goes away, say), the batch it was
in is rolled back and the run stops there. Everything before that batch is
committed and stays removed. The counts printed are of what was removed, the
error is named on standard error by its kind only (its text can hold the
connection string), and the exit status is `1`. An account is removed
entirely or not at all. The next run does the rest.

**Beside the application.** The purge follows the order of locks that
erasing does (`app/services/erasure.py`) and adds none of its own:

- A batch locks its rows `FOR UPDATE SKIP LOCKED`. It never waits for a row,
  so it cannot be part of a deadlock over rows, and a row that somebody else
  holds at that moment is left for the next run. Whether a row is of no use
  is decided with the lock held: a counter that a request has just started
  again is seen as it now is, and kept.
- A batch of stories takes the erasure lock first. Removing a story removes
  its views, and deleting an account removes the views that account made;
  the two touch the same rows of `story_views` in orders of their own.
  Without the lock, one of 30 trial runs of a batch of 400 stories beside
  ten viewers deleting their accounts ended with PostgreSQL aborting two of
  the transactions as deadlocked, which for a user is a `500`. With it, none
  did. So a post or account being deleted and a batch of stories
  come one after the other, each waiting for the other for one batch at most.
  Sessions, tokens and counters take no such lock: removing one of them
  removes nothing else.
- An account is locked `FOR UPDATE`, as when its user deletes it, and passed
  over if it is held, which means that its link is being opened or asked
  for. Whether it is abandoned is asked again once it is held, so a link
  issued a moment before is seen and the account is kept.

What that comes to, as tested with real transactions
(`tests/test_retention_concurrency.py`):

| At the same moment | Outcome |
| --- | --- |
| An account being deleted that has expired stories, ended sessions and spent tokens | Both finish. Each such row is removed once, by whichever comes first. Nothing of the account is left |
| Viewers deleting their accounts while the stories they viewed are purged | Every deletion `204`, every story and view removed |
| A post being deleted or erased, threads being taken apart | Every deletion `204`. No tombstone is left that nothing answers, no post is removed by the purge |
| A verification link asked for after the purge found the account abandoned (these four with `PURGE_UNVERIFIED_ACCOUNTS` on) | The account is kept, the link works |
| A verification link being opened as it expires | The account is verified and kept |
| A link opened while its account is being purged | `400`, the answer for a link that does not exist |
| Registration of a name whose account is being purged | `409`, or `201` once the purge has committed |
| A counter that a request is starting again, not yet committed | The purge passes it over without waiting; the count stands |
| A failed login under counters that the purge has removed and not yet committed | The login waits, then `401`, counted as the first of a new window |
| A story being viewed as it expires | The view is recorded; the story goes with a later run |
| Several purges at once | Every row is removed once; the counts add up to exactly what there was |

Two runs at the same time are therefore safe, and no faster than one.

Two things in this are not forced by a test, because nothing in the
application can be stopped at the moment they need. The deadlock between a
batch of stories and a viewer's deletion depends on the order in which
PostgreSQL comes across rows inside single statements; it was seen in the
trial above and cannot be produced on demand. What is tested for certain is
that the erasure lock keeps the two apart, in both orders. And a request
that starts a counter again after the purge's statement has begun and before
it reaches the row relies on PostgreSQL testing a row again once it is
locked (the behaviour of a locking read at READ COMMITTED); that case is
only covered by sending both at once, repeatedly.

### Measured

On 25,500 accounts, 300,000 posts and 200,000 likes, with a backlog that had
never been purged: 5,000 abandoned accounts, 100,000 expired stories with
500,000 views, 120,000 ended sessions, 100,000 spent tokens and 190,000
finished counters (515,000 rows, and the views).

| | `PURGE_BATCH_SIZE` 100 | 1000 | 10000 |
| --- | --- | --- | --- |
| The whole backlog | 50 s | 33 s | 30 s |
| of which the 5,000 accounts (with `PURGE_UNVERIFIED_ACCOUNTS` on) | 24 s | 24 s | 24 s |
| 100,000 stories and their views | 6.4 s | 2.3 s | 1.9 s |
| 120,000 sessions | 5.5 s | 1.6 s | 1.1 s |
| 100,000 tokens | 4.1 s | 1.1 s | 0.9 s |
| 190,000 counters | 10.2 s | 4.2 s | 1.9 s |
| Longest batch of stories (how long the erasure lock is held) | 29 ms | 82 ms | 258 ms |
| A post or account deletion that arrives during the stories, waiting for the erasure lock | | 12 ms median, 62 ms at most | 170 ms median, 278 ms at most |
| Writing a post during the run | | 2 ms median, as without it | 2 ms median |
| One day's rows afterwards (30 accounts, 3,000 stories, 2,000 sessions, 500 tokens, 5,000 counters) | | 0.45 s | |
| A run with nothing to remove | | 0.08 s | |

Larger batches are faster and hold their locks for longer, and past 1,000
there is little left to gain: that is why 1,000 is the default. A nightly
run has a day's rows to remove and not a backlog.

Accounts cost 5 ms each, whatever the batch size, because each is a
transaction of its own that goes through the whole of account erasure. So an
account is never half removed, and a deletion that arrives meanwhile gets
the erasure lock as quickly as when no purge is running (1.5 ms median). A
backlog of thousands takes seconds more for it.

No index was added and no migration is needed. Only `sessions.expires_at`
has an index that the purge can use; for the rest a batch reads its table in
whatever order the rows come. At the sizes above that is a few milliseconds
per batch. On tables some orders of magnitude larger the purge would want
indexes on the columns it reads (`stories.expires_at`, `sessions.revoked_at`,
the tokens' `expires_at`, `rate_limits.window_start`), at the price of
maintaining them on every write.

### What it does not do

- It is not scheduled by the application. If nobody runs it, nothing is
  removed.
- It has no way to purge one kind of row only.
- It cannot tell that its `RATE_LIMIT_WINDOW_MINUTES` is not the
  application's (see [its environment](#its-environment)).
- `--host` is the address the connection is made to, as the machine running
  the command sees it. It tells two servers apart that have different
  addresses or ports. It does not tell apart what is reached under the same
  address from two machines, `localhost:5432` on each of them, say: where
  that is possible, give the database or the port a name of its own.
- A row that is locked when a batch comes to it is left for the next run.
- It removes rows. The space is given back by PostgreSQL's autovacuum, and
  copies in backups and in the write-ahead log are not touched.
- It does not look for accounts that were verified and then abandoned, or
  for old posts. Nothing here can tell an abandoned account from a quiet
  one.

## Data protection

What the application does with the personal data it holds, set beside the
principles of the GDPR that each mechanism supports. These are technical
mechanisms. They are not a claim of legal compliance, which depends on much
that no application decides: who the controller is, on what grounds data is
processed, what users are told, how requests are handled, and what happens
to backups and logs.

| Mechanism | Supports |
| --- | --- |
| [Data export](#data-export): everything of an account's own, complete, machine-readable, on request and behind the password | Right of access (Art. 15), right to data portability (Art. 20) |
| [Post deletion](#deletion) and [account deletion](#deleting-an-account): real removal, at once, with nothing kept to restore from; what stays of a thread holds no author and no text | Right to erasure (Art. 17) |
| No address and no user agent is stored; tokens only as hashes; rate-limit keys only as keyed hashes; an export leaves out what is not the user's | Data minimisation (Art. 5(1)(c)) |
| Password hashing, session handling, rate limiting, and the password asked again before data is handed out or removed | Security of processing (Art. 32) |
| [Retention purge](#retention): expired stories, ended sessions, spent links and finished rate-limit windows are removed after a configured time, and accounts that were never verified where that is switched on | Storage limitation (Art. 5(1)(e)) |

The retention purge needs to be run to do anything: the application does not
start it. Its periods are engineering defaults that an operator sets, not
periods taken from any law, and it removes rows from the database only.
Posts and verified accounts are kept until their user deletes them, however
old they are, and backups, the write-ahead log and server logs are outside
the application. Running the purge does not by itself make a deployment
compliant with the GDPR or with anything else.

## Tests

```bash
pytest
```

The tests use the database from `DATABASE_URL` and need the migrations applied.
They are meant for a database of their own, not the development one:

```bash
docker exec hopsnop-postgres createdb -U hopsnop hopsnop_test    # once
DATABASE_URL=postgresql+psycopg://hopsnop:hopsnop@localhost:5433/hopsnop_test alembic upgrade head
DATABASE_URL=postgresql+psycopg://hopsnop:hopsnop@localhost:5433/hopsnop_test pytest
```

To start over with it, for instance after a run that was interrupted and
left rows behind, drop it (`dropdb` in place of `createdb`) and repeat the
three steps.

**The tests are not run against a database that has anything in it.** They
assume empty tables, and some of them delete on purpose. Before the first
test every table of the application is checked, and if one holds a row the
run is refused, naming the database and the tables; nothing is deleted to
make room (`tests/database_guard.py`). After the last test the check is made
again, and a run that left rows behind is failed.

Each test runs in a transaction that is rolled back, so nothing is committed.
The exception is the tests of what happens when requests arrive together
(the last part of `test_rate_limiting.py`, `test_auth_concurrency.py`,
`test_post_erasure_concurrency.py`, `test_account_deletion_concurrency.py`,
`test_data_export_concurrency.py`, `test_retention_concurrency.py`, and some
of `test_lifecycle_foundations.py`). They need real transactions:
they commit
accounts and counters under random names and delete them again. What they
share is in `tests/concurrency.py`.

`test_post_erasure_migration.py` tests a migration, which needs a database at
an earlier revision. It creates databases of its own for that
(`hopsnop_migration_…`), runs Alembic against them and drops them again; the
database user therefore needs the right to create databases, and the tests
are skipped where it has none.
The API tests run the application inside the one transaction and always use
the production settings, whatever `ENVIRONMENT` is set to locally.
