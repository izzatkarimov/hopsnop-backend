# Hopsnop Backend

FastAPI backend for Hopsnop, a secure social media platform.

## Setup

```bash
cp .env.example .env
docker compose up -d                      # PostgreSQL on localhost:5433
pip install -r requirements-dev.txt       # test dependencies
alembic upgrade head                      # create the schema
uvicorn app.main:app --reload
```

## Database

The schema is defined by the SQLAlchemy models in `app/models/` and managed
with Alembic. It currently consists of `users`, `posts`, `likes`, `reposts`,
`follows`, `stories` and `story_views`.

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

## Tests

```bash
pytest
```

The tests use the database from `DATABASE_URL` and need the migrations applied.
Each test runs in a transaction that is rolled back, so nothing is committed.
