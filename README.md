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
- Posts are deleted by setting `posts.deleted_at`; the row stays. A reply
  blocks removal of the row it replies to (`ON DELETE RESTRICT` on
  `posts.parent_post_id`), so a reply never loses its parent.
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

## User profiles

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /users/me` | authenticated | The caller's own profile, with private account fields |
| `PATCH /users/me` | authenticated | Change the caller's own profile |
| `GET /users/{username}` | public | Anyone's public profile |

A profile is the existing `users` row; there is no separate profile table.
`followers_count` and `following_count` are counted from `follows` on every
request and are not stored anywhere.

### Public and private views

The two views are separate schemas in `app/schemas/user.py`, each listing its
fields in full.

- **Public** (`GET /users/{username}`): `id`, `username`, `display_name`, `bio`,
  `avatar_url`, `is_private`, `followers_count`, `following_count`,
  `created_at`. The query selects only these columns, so the private ones are
  never read on this path.
- **Own** (`GET /users/me`): the same, plus `email` and `email_verified_at`.

`password_hash`, `is_active` and `updated_at` are in neither.

The username in the path is matched in its canonical lowercase form. Every
account that is not shown gives the same `404`: one that does not exist, one
that is deactivated, and one whose email address was never verified. A private
account's profile is still shown; `is_private` only marks its future content as
restricted.

### Editing

`PATCH /users/me` changes only the fields that are sent:

| Field | Rule | `null` |
| --- | --- | --- |
| `display_name` | 1 to 50 characters after trimming, any script | rejected |
| `bio` | up to 160 characters of plain text after trimming | clears it |
| `avatar_url` | an absolute `http(s)` URL | clears it |
| `is_private` | `true` or `false` | rejected |

A blank `bio` is stored as no bio. A request with none of these fields is
rejected with `422`. Any other field in the body is ignored, so `username`,
`email`, `is_active` and the rest cannot be changed here; the service also
refuses to write any column outside these four.

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
| `DELETE /posts/{post_id}` | author | Delete the post (soft deletion) |
| `POST /posts/{post_id}/like` | authenticated | Like the post |
| `DELETE /posts/{post_id}/like` | authenticated | Take the like back |
| `POST /posts/{post_id}/repost` | authenticated | Repost the post |
| `DELETE /posts/{post_id}/repost` | authenticated | Take the repost back |
| `GET /users/{username}/posts` | public | A user's posts, newest first |

"Public" means no session is needed for a public account's posts. The answer
still depends on who is asking, so these responses are sent with
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

Who may see a post is decided in one place, `_visible_posts` in
`app/services/posts.py`. Every read, and every check that a post exists for
someone, goes through it.

- A post is visible if it is not deleted, its author's account is shown at all
  (active and verified, the same rule as for profiles), and the author allows
  the viewer to read their posts.
- A **public** account's posts can be read by anyone.
- A **private** account's posts can be read only by that account. This is
  temporary: once following exists, approved followers are added to
  `_posts_readable_by`, and every endpoint picks that up.

The account that decides is always the post's own author. A private account's
reply to a public post is private; a public account's reply stays visible when
the post it answers is deleted or becomes private.

A post the caller may not see is answered like one that does not exist:
`404 Post not found`, from the same single query. That holds for writes too.
`PATCH` or `DELETE` on a hidden post is a `404`, never a `403`, so a write
attempt cannot confirm that an id belongs to a post. `403` is only given for a
post the caller can read but did not write.

`GET /users/{username}/posts` answers `404 User not found` for the accounts
whose profile is not shown. For a private account, whose profile is public, it
answers `403` to everyone but the owner, whether or not the account has posts.

### Editing

Only the author can edit, only `content`, and only while the post is less than
60 minutes old. The deadline is `created_at` plus 60 minutes; it is computed,
not stored, and editing does not move it. At exactly 60 minutes the post is
locked, and `PATCH` answers `409`.

`updated_at` is the time of the last edit and equals `created_at` for a post
that was never edited. Nothing else changes it, including deletion.

### Deletion

`DELETE` sets `deleted_at` and returns `204`. The row is kept, together with
its likes, reposts and replies. From then on the post is not found by anyone,
its author included: it cannot be read, edited, replied to, or deleted again
(`404`).

### Replies

`parent_post_id` must name a post the author can currently see. A parent that
does not exist, was deleted, or is hidden gives the same `404 Parent post not
found`. The parent of a post cannot be changed afterwards.

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

Only a post the caller can read can be liked or reposted, replies included,
and a private account's own posts by that account. Any other post, whether
hidden, deleted or nonexistent, is `404 Post not found`, from the same lookup
as reading it, so an attempt reveals nothing about a hidden post. That also
holds for taking back: the likes and reposts of a post that is deleted or
becomes hidden stay in the database with it, are reported to nobody, and are
shown again if the post becomes readable again.

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
| `401` | No usable session, on an endpoint that needs one |
| `403` | Not the author of the post; reading a private account's posts; unverified email; cross-site request |
| `404` | Post, parent post or user not found, or not visible to the caller |
| `409` | The 60-minute edit window has passed |
| `422` | Invalid `content`, `parent_post_id`, `post_id` or `limit` |

## Feed

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /feed` | public | For You: the posts of public accounts, newest first |

For You is, for now, deliberately not a recommendation. It is public content in
chronological order: every post of every public account, newest first. Nothing
is ranked, and likes, reposts and follows play no part in it.

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

A post is in the feed if the caller may see it under the
[visibility rules](#visibility) (not deleted, author active and verified) and
its author's account is public. The second condition is the feed's own, added
by `list_for_you_feed` on top of `_visible_posts`:

- A private account's posts are in the feed for nobody, the account itself
  included. The owner still reads them through `GET /posts/{post_id}` and
  `GET /users/{username}/posts`.
- No session is needed, and the answer is currently the same with or without
  one. The caller is still passed to `_visible_posts`, so a rule added there
  later applies to the feed without a change here.
- Replies are posts. Each appears at its own place in time with its
  `parent_post_id`; nothing is grouped into conversations. A public account's
  reply stays in the feed when the post it answers is deleted or becomes
  private, and only the parent's id is shown, as on every other endpoint.

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
the scan cannot skip cheaply is a long run of consecutive posts by private or
deactivated accounts, since that is decided in `users`; in the same measurement
5,000 such posts in a row cost under 2 ms.

## Tests

```bash
pytest
```

The tests use the database from `DATABASE_URL` and need the migrations applied.
Each test runs in a transaction that is rolled back, so nothing is committed.
The API tests run the application inside that same transaction and always use
the production settings, whatever `ENVIRONMENT` is set to locally.

Several tests assume empty tables, so they can fail if the development database
contains accounts with the usernames the tests use (`alice`, `bob`), or any
posts, which would appear in the feed.
