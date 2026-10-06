"""Security properties of likes and reposts.

What the four endpoints give away about posts that are hidden from the
caller, whose interaction a request can touch, and what no response may ever
contain.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db.session import engine
from app.main import app
from app.models import (
    EmailVerificationToken,
    Like,
    PasswordResetToken,
    Post,
    Repost,
    User,
    UserSession,
)
from app.schemas.post import LikeResponse, RepostResponse
from app.services import posts as posts_service
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    keys_in,
    log_in,
    recorded_selects,
    registration,
    token_from,
)

FOREIGN_ORIGIN = "https://evil.example"
NOT_FOUND = {"detail": "Post not found."}
KINDS = ["like", "repost"]
MODELS = {"like": Like, "repost": Repost}
# The answer to a change, by kind: is it there now, and how many are there?
ANSWER_KEYS = {
    "like": ("liked", "like_count"),
    "repost": ("reposted", "repost_count"),
}
# Account, session and row data that must not travel with an interaction.
NEVER_IN_AN_INTERACTION = {
    "email",
    "email_verified_at",
    "is_active",
    "is_private",
    "password_hash",
    "user_id",
    "post_id",
    "author_id",
    "created_at",
    "deleted_at",
    "session_id",
    "users",
    "likes",
    "reposts",
}


@pytest.fixture(params=KINDS)
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(params=["POST", "DELETE"])
def method(request: pytest.FixtureRequest) -> str:
    return request.param


def change(client: TestClient, method: str, kind: str, post_id: object, **kwargs):
    return client.request(method, f"/posts/{post_id}/{kind}", **kwargs)


def answer(kind: str, active: bool, count: int) -> dict:
    state, counter = ANSWER_KEYS[kind]
    return {state: active, counter: count}


def all_interactions(session: Session) -> set[tuple[str, uuid.UUID, uuid.UUID]]:
    """Every like and repost in the database, as (kind, user, post)."""
    return {
        (kind, row.user_id, row.post_id)
        for kind, model in MODELS.items()
        for row in session.execute(select(model.user_id, model.post_id))
    }


def add_interaction(session: Session, kind: str, user: User, post: Post) -> None:
    session.add(MODELS[kind](user_id=user.id, post_id=post.id))
    session.flush()


def make_private(session: Session, user: User) -> None:
    user.is_private = True
    session.flush()


def hidden_posts(session: Session) -> dict[str, Post]:
    """One post for each reason a post is hidden, each with a like and a repost."""
    authors = {
        "private": add_user(session, "hidden_private"),
        "deleted": add_user(session, "hidden_deleted"),
        "inactive": add_user(session, "hidden_inactive", active=False),
        "unverified": add_user(session, "hidden_unverified", verified=False),
    }
    make_private(session, authors["private"])
    posts = {
        reason: add_post(
            session, author, f"Hidden: {reason}", deleted=reason == "deleted"
        )
        for reason, author in authors.items()
    }
    fan = add_user(session, "hidden_fan")
    for post in posts.values():
        add_interaction(session, "like", fan, post)
        add_interaction(session, "repost", fan, post)
    return posts


@contextmanager
def recorded_statements() -> Iterator[list[str]]:
    """Collects every statement sent to the database inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- the surface ---------------------------------------------------------


def test_the_only_interaction_routes_are_the_intended_ones() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "like" in path or "repost" in path
        for method in operations
    }

    assert routes == {
        ("POST", "/posts/{post_id}/like"),
        ("DELETE", "/posts/{post_id}/like"),
        ("POST", "/posts/{post_id}/repost"),
        ("DELETE", "/posts/{post_id}/repost"),
    }


def test_interaction_requests_take_the_post_from_the_path_and_nothing_else() -> None:
    paths = app.openapi()["paths"]

    for kind in KINDS:
        for operation in paths[f"/posts/{{post_id}}/{kind}"].values():
            assert [param["name"] for param in operation["parameters"]] == ["post_id"]
            assert "requestBody" not in operation


def test_documented_answers_match_the_schemas() -> None:
    schemas = app.openapi()["components"]["schemas"]
    paths = app.openapi()["paths"]

    assert set(LikeResponse.model_fields) == {"liked", "like_count"}
    assert set(RepostResponse.model_fields) == {"reposted", "repost_count"}
    assert set(schemas["LikeResponse"]["properties"]) == {"liked", "like_count"}
    assert set(schemas["RepostResponse"]["properties"]) == {
        "reposted",
        "repost_count",
    }
    for kind, schema in (("like", "LikeResponse"), ("repost", "RepostResponse")):
        for operation in paths[f"/posts/{{post_id}}/{kind}"].values():
            documented = operation["responses"]["200"]["content"]["application/json"]
            assert documented["schema"] == {"$ref": f"#/components/schemas/{schema}"}


@pytest.mark.parametrize("unsupported", ["GET", "PUT", "PATCH"])
def test_interaction_can_only_be_made_or_taken_back(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
    unsupported: str,
) -> None:
    # In particular it cannot be fetched: there is no row to look at.
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)
    before = all_interactions(session)

    response = change(bob_client, unsupported, kind, post.id, json={"liked": False})

    assert response.status_code == 405
    assert all_interactions(session) == before


@pytest.mark.parametrize(
    "path",
    [
        "/posts/{post}/likes",
        "/posts/{post}/reposts",
        "/posts/{post}/likers",
        "/posts/{post}/like/{user}",
        "/posts/{post}/repost/{user}",
        "/users/bob/likes",
        "/users/bob/reposts",
        "/likes",
        "/reposts",
        "/me/likes",
    ],
)
def test_nobody_can_list_who_interacted_or_what_someone_interacted_with(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    path: str,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, "like", bob_account, post)
    add_interaction(session, "repost", bob_account, post)

    # Not even the author of the post.
    response = alice_client.get(path.format(post=post.id, user=bob_account.id))

    assert response.status_code == 404
    assert str(bob_account.id) not in response.text


# --- posts that are hidden from the caller -------------------------------


def test_hidden_post_is_answered_exactly_like_one_that_does_not_exist(
    bob_client: TestClient, session: Session, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)
    before = all_interactions(session)

    missing = change(bob_client, method, kind, uuid.uuid4())
    answers = [change(bob_client, method, kind, post.id) for post in hidden.values()]
    reading = bob_client.get(f"/posts/{hidden['private'].id}")

    # Not "forbidden", not "private", not "deleted": nothing about the answer
    # says that there is a post behind the id, let alone what it has.
    for response in answers:
        assert response.status_code == missing.status_code == 404
        assert response.text == missing.text
        assert sorted(response.headers) == sorted(missing.headers)
    assert missing.json() == NOT_FOUND
    assert missing.text == reading.text
    assert all_interactions(session) == before


def test_hidden_post_takes_the_same_statements_as_one_that_does_not_exist(
    bob_client: TestClient, session: Session, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)
    targets = [uuid.uuid4(), hidden["private"].id, hidden["deleted"].id]

    recorded = []
    for post_id in targets:
        with recorded_statements() as statements:
            assert change(bob_client, method, kind, post_id).status_code == 404
        # Savepoints belong to the test's transaction, not to the request.
        recorded.append([s for s in statements if "SAVEPOINT" not in s])

    # The same work in each case, and none of it a write: the lookup of the
    # session and the lookup of the post, which finds nothing.
    assert recorded[0] == recorded[1] == recorded[2]
    selects = [s for s in recorded[0] if s.startswith("SELECT")]
    assert len(selects) == 2
    assert not any(s.startswith(("INSERT", "DELETE", "UPDATE")) for s in recorded[0])


def test_attempts_on_hidden_posts_cannot_be_told_apart_by_what_was_there_before(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
    method: str,
) -> None:
    # Bob interacted with one of the two posts while he could still read it.
    with_his = add_post(session, alice_account, "Hidden: with his")
    without_his = add_post(session, alice_account, "Hidden: without his")
    add_interaction(session, kind, bob_account, with_his)
    make_private(session, alice_account)
    before = all_interactions(session)

    answers = [
        change(bob_client, method, kind, post.id) for post in (with_his, without_his)
    ]

    assert [response.status_code for response in answers] == [404, 404]
    assert answers[0].text == answers[1].text
    assert all_interactions(session) == before


def test_anonymous_attempt_says_nothing_about_the_post(
    client: TestClient, session: Session, alice_account: User, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)
    public = add_post(session, alice_account)
    targets = [uuid.uuid4(), public.id, hidden["private"].id, hidden["deleted"].id]

    answers = [change(client, method, kind, post_id) for post_id in targets]

    # Refused before any post is looked at.
    assert {response.status_code for response in answers} == {401}
    assert len({response.text for response in answers}) == 1


def test_hidden_posts_interactions_are_reported_to_nobody_who_cannot_read_it(
    make_client, session: Session, bob_account: User
) -> None:
    hidden = hidden_posts(session)
    bob = make_client()
    log_in(bob, "bob")

    for viewer in (make_client(), bob):
        responses = [
            viewer.get("/feed"),
            *(viewer.get(f"/posts/{post.id}") for post in hidden.values()),
            *(
                change(viewer, method, kind, post.id)
                for post in hidden.values()
                for kind in KINDS
                for method in ("POST", "DELETE")
            ),
        ]
        for response in responses:
            assert "count" not in response.text
            assert "liked" not in response.text
            assert "reposted" not in response.text
            assert "Hidden" not in response.text


# --- whose interaction a request can touch -------------------------------


def test_interaction_is_always_the_signed_in_users(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    post = add_post(session, alice_account)
    alice_id = str(alice_account.id)

    # Every way a client might try to act as someone else.
    response = bob_client.post(
        f"/posts/{post.id}/{kind}?user_id={alice_id}&as=alice",
        json={"user_id": alice_id, "username": "alice", "liked_by": [alice_id]},
        headers={"X-User-Id": alice_id},
    )

    assert response.status_code == 200
    assert all_interactions(session) == {(kind, bob_account.id, post.id)}


def test_another_users_interaction_cannot_be_taken_back(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, alice_account, post)
    alice_id = str(alice_account.id)
    before = all_interactions(session)

    response = bob_client.request(
        "DELETE",
        f"/posts/{post.id}/{kind}?user_id={alice_id}&as=alice",
        json={"user_id": alice_id, "username": "alice"},
        headers={"X-User-Id": alice_id},
    )

    # Answered for bob, who had none. Alice's is untouched.
    assert response.status_code == 200
    assert response.json() == answer(kind, False, 1)
    assert all_interactions(session) == before


def test_target_is_only_ever_the_post_in_the_path(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
    method: str,
) -> None:
    in_path = add_post(session, alice_account, "In the path")
    elsewhere = add_post(session, alice_account, "Named elsewhere")
    add_interaction(session, kind, bob_account, elsewhere)
    other = str(elsewhere.id)

    response = bob_client.request(
        method,
        f"/posts/{in_path.id}/{kind}?post_id={other}&id={other}",
        json={"post_id": other, "id": other, "posts": [other]},
    )

    assert response.status_code == 200
    made = method == "POST"
    assert response.json() == answer(kind, made, int(made))
    assert (kind, bob_account.id, elsewhere.id) in all_interactions(session)
    assert ((kind, bob_account.id, in_path.id) in all_interactions(session)) is made


def test_client_cannot_dictate_the_state_or_the_count(
    bob_client: TestClient, session: Session, alice_account: User, kind: str
) -> None:
    post = add_post(session, alice_account)
    state, counter = ANSWER_KEYS[kind]
    claims = {state: False, counter: 1000, "count": 1000, f"{state}_by_me": False}

    made = bob_client.post(f"/posts/{post.id}/{kind}", json=claims)
    read = bob_client.get(f"/posts/{post.id}?{counter}=1000").json()

    assert made.json() == answer(kind, True, 1)
    assert read[counter] == 1
    assert len(all_interactions(session)) == 1


def test_one_user_can_only_ever_add_one_to_a_count(
    bob_client: TestClient, session: Session, alice_account: User, kind: str
) -> None:
    post = add_post(session, alice_account)

    for _ in range(25):
        assert change(bob_client, "POST", kind, post.id).status_code == 200

    assert bob_client.get(f"/posts/{post.id}").json()[ANSWER_KEYS[kind][1]] == 1
    assert session.scalar(select(func.count()).select_from(MODELS[kind])) == 1


def test_interaction_touches_nothing_but_its_own_row(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: str,
    method: str,
) -> None:
    post = add_post(session, alice_account, "Untouched")
    stored = select(Post.__table__).where(Post.id == post.id)
    before = dict(session.execute(stored).one()._mapping)

    with recorded_statements() as statements:
        assert change(bob_client, method, kind, post.id).status_code == 200

    # The post is not edited by being liked: updated_at marks edits only.
    assert dict(session.execute(stored).one()._mapping) == before
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    table = MODELS[kind].__tablename__
    assert len(writes) == 1
    assert writes[0].startswith((f"INSERT INTO {table} ", f"DELETE FROM {table} "))


# --- CSRF ----------------------------------------------------------------


def test_cross_site_interaction_is_rejected_and_has_no_effect(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
    method: str,
) -> None:
    liked = add_post(session, alice_account, "Already liked")
    not_liked = add_post(session, alice_account, "Not liked")
    add_interaction(session, kind, bob_account, liked)
    before = all_interactions(session)

    for post in (liked, not_liked):
        response = change(
            bob_client, method, kind, post.id, headers={"Origin": FOREIGN_ORIGIN}
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-origin request rejected."}

    assert all_interactions(session) == before


def test_cross_site_attempt_is_refused_before_the_post_is_looked_at(
    bob_client: TestClient, session: Session, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)

    answers = [
        change(bob_client, method, kind, post_id, headers={"Origin": FOREIGN_ORIGIN})
        for post_id in (uuid.uuid4(), hidden["private"].id)
    ]

    assert [response.status_code for response in answers] == [403, 403]
    assert answers[0].text == answers[1].text


def test_reading_never_makes_or_removes_an_interaction(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, "like", bob_account, post)
    bob = make_client()
    log_in(bob, "bob")
    before = all_interactions(session)

    for viewer in (make_client(), bob):
        viewer.get(f"/posts/{post.id}")
        viewer.get("/users/alice/posts")
        viewer.get("/feed")
        for kind in KINDS:
            viewer.get(f"/posts/{post.id}/{kind}")  # 405

    assert all_interactions(session) == before


# --- what responses may contain ------------------------------------------


def test_posts_do_not_say_who_interacted_with_them(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account, "Hello Hopsnop!")
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    for kind in KINDS:
        assert change(bob, "POST", kind, post.id).status_code == 200

    # Not to the public, not to the author, and not to bob himself beyond
    # "you did": only numbers leave the likes and reposts tables.
    for viewer in (make_client(), alice, bob):
        responses = [
            viewer.get(f"/posts/{post.id}"),
            viewer.get("/users/alice/posts"),
            viewer.get("/feed"),
        ]
        for response in responses:
            assert response.status_code == 200
            assert str(bob_account.id) not in response.text
            assert "bob" not in response.text.lower()


def test_interaction_answer_identifies_no_one(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, alice_account, post)

    response = change(bob_client, method, kind, post.id)

    assert set(response.json()) == set(ANSWER_KEYS[kind])
    for value in (alice_account.id, bob_account.id, post.id, "alice", "bob"):
        assert str(value) not in response.text


def test_no_interaction_response_contains_a_secret(
    make_client, session: Session, alice_account: User, bob_account: User, outbox
) -> None:
    """Walks through every interaction endpoint and inspects all that came back."""
    alice, bob, anonymous = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    # A verification token and a password reset token are outstanding.
    registered = anonymous.post(
        "/auth/register",
        json=registration(username="carol", email="carol@example.com"),
    )
    assert registered.status_code == 201
    anonymous.post("/auth/forgot-password", json={"email": "bob@example.com"})
    verification_token = token_from(outbox.verification[0][1])
    reset_token = token_from(outbox.password_reset[0][1])
    carol = session.scalars(select(User).where(User.username == "carol")).one()

    post = add_post(session, alice_account, "Hello Hopsnop!")
    hidden = hidden_posts(session)
    session_tokens = [
        client.cookies.get("hopsnop_session") for client in (alice, bob)
    ]
    responses = []

    def call(who: TestClient, method: str, path: str, **kwargs: object) -> None:
        responses.append(who.request(method, path, **kwargs))

    for kind in KINDS:
        own = f"/posts/{post.id}/{kind}"
        call(bob, "POST", own)
        call(bob, "POST", own)  # again
        call(alice, "POST", own)
        call(bob, "DELETE", own)
        call(bob, "DELETE", own)  # again
        call(anonymous, "POST", own)  # 401
        call(anonymous, "DELETE", own)  # 401
        call(bob, "POST", f"/posts/{hidden['private'].id}/{kind}")  # 404
        call(bob, "DELETE", f"/posts/{hidden['deleted'].id}/{kind}")  # 404
        call(bob, "POST", f"/posts/{uuid.uuid4()}/{kind}")  # 404
        call(bob, "POST", f"/posts/not-an-id/{kind}")  # 422
        # A secret sent in is not sent back either.
        call(bob, "POST", f"/posts/{session_tokens[1]}/{kind}")  # 422
        call(bob, "GET", own)  # 405
        call(bob, "POST", own, headers={"Origin": FOREIGN_ORIGIN})  # 403
    for viewer in (anonymous, alice, bob):
        call(viewer, "GET", f"/posts/{post.id}")
        call(viewer, "GET", "/users/alice/posts")
        call(viewer, "GET", "/feed")

    seen = {response.status_code for response in responses}
    assert seen == {200, 401, 403, 404, 405, 422}

    token_hashes = [
        *session.scalars(select(UserSession.token_hash)),
        *session.scalars(select(EmailVerificationToken.token_hash)),
        *session.scalars(select(PasswordResetToken.token_hash)),
    ]
    session_ids = [str(id) for id in session.scalars(select(UserSession.id))]
    secrets = [
        *session_tokens,
        verification_token,
        reset_token,
        *token_hashes,
        *session_ids,
        alice_account.password_hash,
        carol.password_hash,
        alice_account.email,
        bob_account.email,
        carol.email,
    ]
    assert len(token_hashes) == 4
    assert all(secrets)

    for response in responses:
        found = keys_in(response.json())
        assert found.isdisjoint(SENSITIVE_KEYS), response.url
        if response.request.method != "GET":
            assert found.isdisjoint(NEVER_IN_AN_INTERACTION), response.url
            if response.status_code == 200:
                assert found in (set(ANSWER_KEYS["like"]), set(ANSWER_KEYS["repost"]))
        for secret in secrets:
            assert secret not in response.text, response.url
        assert "Hidden" not in response.text, response.url
        assert "set-cookie" not in response.headers, response.url
        for name, value in response.headers.items():
            for secret in secrets:
                assert secret not in value, (response.url, name)


# --- errors --------------------------------------------------------------


def test_every_interaction_error_has_the_same_shape(
    bob_client: TestClient, make_client, session: Session, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)

    errors = [
        change(make_client(), method, kind, uuid.uuid4()),  # 401
        change(bob_client, method, kind, hidden["private"].id),  # 404
        change(bob_client, method, kind, "not-an-id"),  # 422
        change(bob_client, "PUT", kind, uuid.uuid4()),  # 405
        change(
            bob_client, method, kind, uuid.uuid4(), headers={"Origin": FOREIGN_ORIGIN}
        ),  # 403
    ]

    statuses = [response.status_code for response in errors]
    assert statuses == [401, 404, 422, 405, 403]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"
    # None of them is a conflict: nothing about repeating a request is one.
    assert 409 not in statuses


def test_error_details_name_no_internals(
    bob_client: TestClient, session: Session, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)

    responses = [
        change(bob_client, method, kind, hidden["deleted"].id),
        change(bob_client, method, kind, uuid.uuid4()),
        change(bob_client, method, kind, "' OR '1'='1"),
    ]

    for response in responses:
        text = response.text.lower()
        for internal in ("sql", "select", "traceback", "likes", "reposts", "conflict"):
            assert internal not in text


def test_unexpected_error_reveals_nothing_and_leaves_nothing_behind(
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, alice_account, post)
    if method == "DELETE":
        add_interaction(session, kind, bob_account, post)
    before = all_interactions(session)
    made_by = posts_service._made_by

    def fail_once_the_change_is_made(model, post_id, viewer):
        # Asked about one post by its id, as it is after the write, rather
        # than about the posts of a query, as it is while looking the post up.
        if isinstance(post_id, uuid.UUID):
            raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")
        return made_by(model, post_id, viewer)

    monkeypatch.setattr(posts_service, "_made_by", fail_once_the_change_is_made)
    client = make_client(raise_server_exceptions=False)
    log_in(client, "bob")

    response = change(client, method, kind, post.id)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "postgresql" not in response.text
    # Nothing was committed: a request that fails changes nothing.
    assert all_interactions(session) == before


def test_interaction_and_post_deletion_do_not_resurrect_anything(
    alice_client: TestClient, bob_client: TestClient, session: Session, kind: str
) -> None:
    post = alice_client.post("/posts", json={"content": "Short-lived"}).json()
    assert change(bob_client, "POST", kind, post["id"]).status_code == 200
    assert alice_client.delete(f"/posts/{post['id']}").status_code == 204
    deleted_at = session.scalar(select(Post.deleted_at))

    for method in ("POST", "DELETE"):
        assert change(bob_client, method, kind, post["id"]).status_code == 404

    assert isinstance(deleted_at, datetime)
    assert deleted_at <= datetime.now(timezone.utc)
    assert session.scalar(select(Post.deleted_at)) == deleted_at
    assert bob_client.get(f"/posts/{post['id']}").status_code == 404


def test_hidden_lookup_is_one_statement_like_reading_the_post(
    bob_client: TestClient, session: Session, kind: str, method: str
) -> None:
    hidden = hidden_posts(session)
    post_id = hidden["private"].id
    session.expunge_all()

    with recorded_selects() as reading:
        assert bob_client.get(f"/posts/{post_id}").status_code == 404
    with recorded_selects() as changing:
        assert change(bob_client, method, kind, post_id).status_code == 404

    # The very statement that reading the post runs, and nothing after it.
    assert changing == reading
