"""Security properties of following.

Whose follow a request can touch, what the endpoints give away about accounts
that are not shown, that the writes are protected like every other write, and
what no response may ever contain.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import engine
from app.main import app
from app.models import (
    EmailVerificationToken,
    Follow,
    PasswordResetToken,
    User,
    UserSession,
)
from app.schemas.user import FollowResponse, UserPageResponse, UserSummaryResponse
from app.services import users as users_service
from helpers import (
    SENSITIVE_KEYS,
    add_user,
    columns,
    keys_in,
    log_in,
    registration,
    token_from,
)

FOREIGN_ORIGIN = "https://evil.example"
OWN_ORIGIN = "https://testserver"
CROSS_ORIGIN = {"detail": "Cross-origin request rejected."}
USER_NOT_FOUND = {"detail": "User not found."}
# Every key a list of users is made of.
LIST_KEYS = {"items", "next_cursor", "username", "display_name", "avatar_url"}
# The five follow endpoints, as (method, last segment of the path).
WRITES = [("POST", "follow"), ("DELETE", "follow")]
READS = [("GET", "follow-status"), ("GET", "followers"), ("GET", "following")]
# Account, session and row data that must not travel with a follow.
NEVER_IN_A_FOLLOW = {
    "id",
    "email",
    "email_verified_at",
    "is_active",
    "is_private",
    "bio",
    "password_hash",
    "follower_id",
    "following_id",
    "user_id",
    "created_at",
    "followed_at",
    "session_id",
    "follows",
    "users",
}


@pytest.fixture(params=["POST", "DELETE"])
def method(request: pytest.FixtureRequest) -> str:
    """Following, or unfollowing."""
    return request.param


@pytest.fixture(params=WRITES + READS, ids=lambda endpoint: " ".join(endpoint))
def endpoint(request: pytest.FixtureRequest) -> tuple[str, str]:
    return request.param


def call(client: TestClient, endpoint: tuple[str, str], username: str, **kwargs):
    method, segment = endpoint
    return client.request(method, f"/users/{username}/{segment}", **kwargs)


def change(client: TestClient, method: str, username: str, **kwargs):
    return client.request(method, f"/users/{username}/follow", **kwargs)


def rows(session: Session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Every follow in the database, as (follower, followed)."""
    found = session.execute(select(Follow.follower_id, Follow.following_id)).all()
    return {(row.follower_id, row.following_id) for row in found}


def add_follow(session: Session, follower: User, followed: User) -> None:
    session.add(Follow(follower_id=follower.id, following_id=followed.id))
    session.flush()


def browser(make_client, username: str) -> TestClient:
    client = make_client()
    assert log_in(client, username).status_code == 200
    return client


def add_hidden_accounts(session: Session, *connected_to: User) -> list[User]:
    """One deactivated and one unverified account, both named "hidden_...".

    Each follows, and is followed by, every account in ``connected_to``.
    """
    hidden = [
        add_user(session, "hidden_inactive", active=False),
        add_user(session, "hidden_unverified", verified=False),
    ]
    for account in connected_to:
        for user in hidden:
            add_follow(session, user, account)
            add_follow(session, account, user)
    return hidden


@contextmanager
def recorded_statements() -> Iterator[list[str]]:
    """Collects the statements a request sends to the database."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        # Savepoints belong to the test's transaction, not to the request.
        if "SAVEPOINT" not in statement:
            statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- the surface ---------------------------------------------------------


def test_the_only_follow_routes_are_the_intended_ones() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "follow" in path
        for method in operations
    }

    assert routes == {
        ("POST", "/users/{username}/follow"),
        ("DELETE", "/users/{username}/follow"),
        ("GET", "/users/{username}/follow-status"),
        ("GET", "/users/{username}/followers"),
        ("GET", "/users/{username}/following"),
    }


def test_there_is_nothing_to_request_approve_or_wait_for() -> None:
    document = app.openapi()
    routes = {path: at for path, at in document["paths"].items() if "follow" in path}
    answers = [
        document["components"]["schemas"][schema.__name__]
        for schema in (FollowResponse, UserSummaryResponse, UserPageResponse)
    ]
    documented = (str(routes) + str(answers)).lower()

    # Following is immediate. There is no state between not following and
    # following, anywhere: not in the routes, the answers or the table.
    assert len(routes) == 4
    for word in ("request", "pending", "approv", "accept", "reject", "private"):
        assert word not in documented, word
    assert set(FollowResponse.model_fields) == {"following"}
    assert FollowResponse.model_fields["following"].annotation is bool
    assert {column.key for column in Follow.__table__.columns} == {
        "follower_id",
        "following_id",
        "created_at",
    }


def test_follow_requests_take_the_user_from_the_path_and_nothing_else() -> None:
    operations = app.openapi()["paths"]["/users/{username}/follow"]

    for operation in operations.values():
        assert [param["name"] for param in operation["parameters"]] == ["username"]
        assert "requestBody" not in operation


def test_documented_answers_match_the_schemas() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert set(UserSummaryResponse.model_fields) == {
        "username",
        "display_name",
        "avatar_url",
    }
    assert set(UserPageResponse.model_fields) == {"items", "next_cursor"}
    for schema in (FollowResponse, UserSummaryResponse, UserPageResponse):
        documented = set(schemas[schema.__name__]["properties"])
        assert documented == set(schema.model_fields)
        assert documented.isdisjoint(SENSITIVE_KEYS | NEVER_IN_A_FOLLOW)


@pytest.mark.parametrize("unsupported", ["GET", "PUT", "PATCH"])
def test_follow_can_only_be_made_or_ended(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    unsupported: str,
) -> None:
    # In particular it cannot be fetched: there is no row to look at.
    add_follow(session, bob_account, alice_account)
    before = rows(session)

    response = change(bob_client, unsupported, "alice", json={"following": False})

    assert response.status_code == 405
    assert rows(session) == before


@pytest.mark.parametrize("unsupported", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("segment", ["follow-status", "followers", "following"])
def test_status_and_lists_can_only_be_read(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    segment: str,
    unsupported: str,
) -> None:
    # Nobody is put on a list, or taken off one, by writing to the list.
    add_follow(session, alice_account, bob_account)
    before = rows(session)

    response = bob_client.request(
        unsupported, f"/users/alice/{segment}", json={"username": "bob"}
    )

    assert response.status_code == 405
    assert rows(session) == before


@pytest.mark.parametrize(
    "path",
    [
        "/users",
        "/followers",
        "/following",
        "/follows",
        "/follow",
        "/users/alice/follows",
    ],
)
def test_there_is_no_list_of_all_users_or_of_all_follows(
    alice_client: TestClient, session: Session, alice_account: User, path: str
) -> None:
    add_follow(session, add_user(session, "carol"), alice_account)

    response = alice_client.get(path)

    assert response.status_code == 404
    assert "carol" not in response.text


# --- whose follow a request can touch ------------------------------------


def test_follower_is_always_the_signed_in_user(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    carol_id = str(carol.id)

    # Every way a client might try to follow on behalf of someone else.
    response = bob_client.post(
        f"/users/alice/follow?follower_id={carol_id}&user_id={carol_id}&as=carol",
        json={"follower_id": carol_id, "follower": "carol", "user_id": carol_id},
        headers={"X-User-Id": carol_id, "X-Follower-Id": carol_id},
    )

    assert response.status_code == 200
    assert response.json() == {"following": True}
    assert rows(session) == {(bob_account.id, alice_account.id)}


def test_another_users_follow_cannot_be_ended(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, carol, alice_account)
    carol_id = str(carol.id)

    response = bob_client.request(
        "DELETE",
        f"/users/alice/follow?follower_id={carol_id}&as=carol",
        json={"follower_id": carol_id, "follower": "carol"},
        headers={"X-User-Id": carol_id},
    )

    # Answered for bob, who was not following. Carol still is.
    assert response.status_code == 200
    assert response.json() == {"following": False}
    assert rows(session) == {(carol.id, alice_account.id)}


def test_nobody_can_be_made_to_follow_the_caller(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    alice_id = str(alice_account.id)

    # Bob wants alice among his followers. All he can do is follow her.
    attempts = [
        bob_client.post("/users/bob/followers", json={"username": "alice"}),
        bob_client.post(
            "/users/alice/follow",
            json={"following_id": str(bob_account.id), "follower_id": alice_id},
        ),
        bob_client.post(f"/users/bob/follow?follower_id={alice_id}"),
    ]

    assert [response.status_code for response in attempts] == [405, 200, 400]
    assert rows(session) == {(bob_account.id, alice_account.id)}


def test_target_is_only_ever_the_user_in_the_path(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, bob_account, carol)
    carol_id = str(carol.id)

    response = bob_client.request(
        method,
        f"/users/alice/follow?username=carol&following_id={carol_id}",
        json={"username": "carol", "following_id": carol_id, "users": ["carol"]},
    )

    made = method == "POST"
    assert response.status_code == 200
    assert response.json() == {"following": made}
    assert (bob_account.id, carol.id) in rows(session)
    assert ((bob_account.id, alice_account.id) in rows(session)) is made


def test_client_cannot_dictate_the_state(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    followed = bob_client.post("/users/alice/follow", json={"following": False})
    unfollowed = bob_client.request(
        "DELETE", "/users/alice/follow", json={"following": True}
    )

    assert followed.json() == {"following": True}
    assert unfollowed.json() == {"following": False}
    assert rows(session) == set()


def test_one_user_can_only_ever_add_one_to_a_count(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    for _ in range(25):
        assert change(bob_client, "POST", "alice").status_code == 200

    assert bob_client.get("/users/alice").json()["followers_count"] == 1
    assert bob_client.get("/users/bob").json()["following_count"] == 1
    assert len(rows(session)) == 1


def test_counts_cannot_be_set_by_the_client(
    bob_client: TestClient, alice_account: User
) -> None:
    claims = {"followers_count": 1_000_000, "following_count": 1_000_000}

    bob_client.post("/users/alice/follow", json=claims)
    bob_client.patch("/users/me", json={"bio": "Hi", **claims})

    assert bob_client.get("/users/alice").json()["followers_count"] == 1
    assert bob_client.get("/users/me").json()["followers_count"] == 0
    assert bob_client.get("/users/me").json()["following_count"] == 1


def test_follow_touches_nothing_but_its_own_row(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    before = [columns(session, user) for user in (alice_account, bob_account)]

    with recorded_statements() as statements:
        assert change(bob_client, method, "alice").status_code == 200

    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(writes) == 1
    assert writes[0].startswith(("INSERT INTO follows ", "DELETE FROM follows "))
    # Neither account is changed by a follow: nothing is counted on them.
    after = [columns(session, user) for user in (alice_account, bob_account)]
    assert after == before


# --- accounts that are not shown -----------------------------------------


def test_account_that_is_not_shown_is_answered_like_one_that_does_not_exist(
    bob_client: TestClient,
    session: Session,
    bob_account: User,
    endpoint: tuple[str, str],
) -> None:
    hidden = add_hidden_accounts(session, bob_account)
    before = rows(session)

    missing = call(bob_client, endpoint, "nonexistent")
    answers = [call(bob_client, endpoint, user.username) for user in hidden]
    profile = bob_client.get("/users/nonexistent")

    # Not "deactivated", not "unverified", not "forbidden": nothing about
    # the answer says that there is an account behind the name, although
    # bob follows both of them and both follow him.
    for response in answers:
        assert response.status_code == missing.status_code == 404
        assert response.text == missing.text
        assert sorted(response.headers) == sorted(missing.headers)
    assert missing.json() == USER_NOT_FOUND
    assert missing.text == profile.text
    assert rows(session) == before


def test_account_that_is_not_shown_takes_the_same_statements_as_a_missing_one(
    bob_client: TestClient,
    session: Session,
    bob_account: User,
    endpoint: tuple[str, str],
) -> None:
    add_hidden_accounts(session, bob_account)

    recorded = []
    for username in ("nonexistent", "hidden_inactive", "hidden_unverified"):
        with recorded_statements() as statements:
            assert call(bob_client, endpoint, username).status_code == 404
        recorded.append(statements)

    # The same work in each case, and none of it a write.
    assert recorded[0] == recorded[1] == recorded[2]
    assert all(statement.startswith("SELECT") for statement in recorded[0])


def test_anonymous_attempt_says_nothing_about_the_account(
    client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    add_hidden_accounts(session)

    answers = [
        change(client, method, username)
        for username in ("nonexistent", "alice", "hidden_inactive", "al")
    ]

    # Refused before any account is looked at.
    assert {response.status_code for response in answers} == {401}
    assert len({response.text for response in answers}) == 1


def test_accounts_that_are_not_shown_leave_no_trace_in_any_list(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    hidden = add_hidden_accounts(session, alice_account, bob_account)
    add_follow(session, bob_account, alice_account)
    bob = browser(make_client, "bob")

    for reader in (make_client(), bob):
        for username in ("alice", "bob"):
            for side in ("followers", "following"):
                for limit in (1, 50):
                    response = reader.get(f"/users/{username}/{side}?limit={limit}")
                    assert response.status_code == 200
                    assert "hidden_" not in response.text
                    for user in hidden:
                        assert user.display_name not in response.text
                    # Not even that there is anyone else.
                    assert response.json()["next_cursor"] is None


def test_counts_are_row_counts_and_say_nothing_about_who(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_hidden_accounts(session, alice_account)

    profile = client.get("/users/alice")

    # A follow by an account that is no longer shown is still a follow, and
    # still counted. Which account it is, the profile does not say and the
    # list does not show.
    body = profile.json()
    assert (body["followers_count"], body["following_count"]) == (2, 2)
    assert "hidden_" not in profile.text
    assert client.get("/users/alice/followers").json()["items"] == []


# --- CSRF ----------------------------------------------------------------


def test_cross_site_follow_is_rejected_and_has_no_effect(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, bob_account, carol)
    before = rows(session)

    # One he does not follow and one he does: neither is changed.
    for username in ("alice", "carol"):
        response = change(
            bob_client, method, username, headers={"Origin": FOREIGN_ORIGIN}
        )
        assert response.status_code == 403
        assert response.json() == CROSS_ORIGIN

    assert rows(session) == before


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "null"},
        {"Origin": "http://testserver"},  # this host, but not over HTTPS
        {"Origin": "https://testserver.evil.example"},
        {"Origin": f"{settings.frontend_origin}.evil.example"},
        {"Referer": f"{FOREIGN_ORIGIN}/users/alice"},
        {"Referer": "https://testserver.evil.example/users/alice"},
        {"Origin": FOREIGN_ORIGIN, "Referer": f"{OWN_ORIGIN}/users/alice"},
    ],
)
def test_untrusted_origin_or_referer_is_rejected(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    method: str,
    headers: dict[str, str],
) -> None:
    response = change(bob_client, method, "alice", headers=headers)

    assert response.status_code == 403
    assert response.json() == CROSS_ORIGIN
    assert rows(session) == set()


@pytest.mark.parametrize(
    "headers",
    [
        {},  # not sent by a browser acting for another site
        {"Origin": OWN_ORIGIN},
        {"Origin": settings.frontend_origin},
        {"Referer": f"{OWN_ORIGIN}/docs"},
        {"Referer": f"{settings.frontend_origin}/alice"},
    ],
)
def test_request_from_a_trusted_origin_is_accepted(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    headers: dict[str, str],
) -> None:
    followed = change(bob_client, "POST", "alice", headers=headers)
    assert followed.status_code == 200
    assert rows(session) == {(bob_account.id, alice_account.id)}

    unfollowed = change(bob_client, "DELETE", "alice", headers=headers)
    assert unfollowed.status_code == 200
    assert rows(session) == set()


def test_cross_site_attempt_is_refused_before_the_account_is_looked_at(
    bob_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    add_hidden_accounts(session)

    answers = [
        change(bob_client, method, username, headers={"Origin": FOREIGN_ORIGIN})
        for username in ("alice", "nonexistent", "hidden_inactive", "bob")
    ]

    assert {response.status_code for response in answers} == {403}
    assert len({response.text for response in answers}) == 1


@pytest.mark.parametrize("segment", ["follow-status", "followers", "following"])
def test_cross_site_read_gets_no_cors_permission(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    segment: str,
) -> None:
    add_follow(session, bob_account, alice_account)

    response = bob_client.get(
        f"/users/alice/{segment}", headers={"Origin": FOREIGN_ORIGIN}
    )

    # Answered, as any GET is, but the browser will not let the other
    # site's page read it.
    assert "access-control-allow-origin" not in response.headers


def test_reading_never_makes_or_ends_a_follow(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    add_follow(session, bob_account, alice_account)
    bob = browser(make_client, "bob")
    before = rows(session)

    for reader in (make_client(), bob):
        for username in ("alice", "bob"):
            reader.get(f"/users/{username}")
            reader.get(f"/users/{username}/follow-status")
            reader.get(f"/users/{username}/followers?limit=1")
            reader.get(f"/users/{username}/following")
            reader.get(f"/users/{username}/follow")  # 405

    assert rows(session) == before


# --- what responses may contain ------------------------------------------


def test_list_is_made_of_the_intended_keys_and_no_others(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    for user in (bob_account, carol):
        add_follow(session, user, alice_account)
        add_follow(session, alice_account, user)
    bob = browser(make_client, "bob")

    for reader in (make_client(), bob):
        for side in ("followers", "following"):
            for limit in (1, 50):
                response = reader.get(f"/users/alice/{side}?limit={limit}")
                found = keys_in(response.json())
                assert found == LIST_KEYS
                assert found.isdisjoint(SENSITIVE_KEYS | NEVER_IN_A_FOLLOW)


def test_list_exposes_nothing_of_a_user_but_the_name_and_avatar(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    bob_account.bio = "Only on the profile"
    add_follow(session, bob_account, alice_account)
    add_follow(session, alice_account, bob_account)
    bob_id = str(bob_account.id)
    alice = browser(make_client, "alice")

    for reader in (make_client(), alice):
        for side in ("followers", "following"):
            response = reader.get(f"/users/alice/{side}")

            assert response.json()["items"] == [
                {"username": "bob", "display_name": "Bob", "avatar_url": None}
            ]
            assert bob_id not in response.text
            assert "@example.com" not in response.text
            assert "argon2" not in response.text
            assert "Only on the profile" not in response.text


def test_follow_answer_identifies_no_one(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    add_follow(session, alice_account, bob_account)

    answers = [
        change(bob_client, method, "alice"),
        bob_client.get("/users/alice/follow-status"),
    ]

    for response in answers:
        assert set(response.json()) == {"following"}
        for value in (alice_account.id, bob_account.id, "alice", "bob"):
            assert str(value) not in response.text


def test_no_follow_response_contains_a_secret(
    make_client, session: Session, alice_account: User, bob_account: User, outbox
) -> None:
    """Walks through every follow endpoint and inspects all that came back."""
    alice, bob, anonymous = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    # A verification token and a password reset token are outstanding.
    registered = anonymous.post(
        "/auth/register",
        json=registration(username="carol", email="carol@example.com"),
    )
    assert registered.status_code == 201
    anonymous.post("/auth/forgot-password", json={"email": "alice@example.com"})
    verification_token = token_from(outbox.verification[0][1])
    reset_token = token_from(outbox.password_reset[0][1])
    carol = session.scalars(select(User).where(User.username == "carol")).one()
    add_hidden_accounts(session, alice_account, bob_account)
    session_tokens = [
        client.cookies.get("hopsnop_session") for client in (alice, bob)
    ]
    responses = []

    def request(who: TestClient, method: str, path: str, **kwargs: object) -> None:
        responses.append(who.request(method, path, **kwargs))

    request(bob, "POST", "/users/alice/follow")
    request(bob, "POST", "/users/alice/follow")  # again
    request(alice, "POST", "/users/bob/follow")
    request(bob, "POST", "/users/bob/follow")  # 400
    request(bob, "POST", "/users/carol/follow")  # 404: not verified
    request(bob, "POST", "/users/hidden_inactive/follow")  # 404
    request(bob, "POST", "/users/nonexistent/follow")  # 404
    request(anonymous, "POST", "/users/alice/follow")  # 401
    request(anonymous, "DELETE", "/users/alice/follow")  # 401
    request(bob, "PUT", "/users/alice/follow")  # 405
    request(bob, "POST", "/users/alice/follow", headers={"Origin": FOREIGN_ORIGIN})
    for viewer in (anonymous, alice, bob):
        for username in ("alice", "bob"):
            request(viewer, "GET", f"/users/{username}")
            request(viewer, "GET", f"/users/{username}/follow-status")
            request(viewer, "GET", f"/users/{username}/followers")
            request(viewer, "GET", f"/users/{username}/following?limit=1")
        request(viewer, "GET", "/users/carol/followers")  # 404
        request(viewer, "GET", "/users/alice/followers?cursor=abc")  # 400
        request(viewer, "GET", "/users/alice/following?limit=0")  # 422
        # A secret sent in is not sent back either.
        request(viewer, "GET", f"/users/alice/followers?cursor={session_tokens[0]}")
        request(viewer, "GET", f"/users/alice/following?limit={reset_token}")
        request(viewer, "GET", f"/users/{verification_token}/follow-status")  # 404
    request(bob, "DELETE", "/users/alice/follow")
    request(bob, "DELETE", "/users/alice/follow")  # again

    seen = {response.status_code for response in responses}
    assert seen == {200, 400, 401, 403, 404, 405, 422}

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
        if "/follow" in response.url.path and response.status_code == 200:
            # A follow answer or a list: never a profile's worth of data.
            assert found in ({"following"}, LIST_KEYS, {"items", "next_cursor"})
        for secret in secrets:
            assert secret not in response.text, response.url
        assert "hidden_" not in response.text, response.url
        assert "carol" not in response.text, response.url
        assert "set-cookie" not in response.headers, response.url
        for name, value in response.headers.items():
            for secret in secrets:
                assert secret not in value, (response.url, name)


# --- errors --------------------------------------------------------------


def test_every_follow_error_has_the_same_shape(
    bob_client: TestClient, make_client, alice_account: User
) -> None:
    errors = [
        change(make_client(), "POST", "alice"),  # 401
        change(bob_client, "POST", "bob"),  # 400
        change(bob_client, "POST", "nonexistent"),  # 404
        change(bob_client, "PUT", "alice"),  # 405
        change(bob_client, "POST", "alice", headers={"Origin": FOREIGN_ORIGIN}),  # 403
        bob_client.get("/users/alice/followers?cursor=abc"),  # 400
        bob_client.get("/users/alice/following?limit=0"),  # 422
        bob_client.get("/users/nonexistent/follow-status"),  # 404
    ]

    statuses = [response.status_code for response in errors]
    assert statuses == [401, 400, 404, 405, 403, 400, 422, 404]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"
    # None of them is a conflict: nothing about repeating a request is one.
    assert 409 not in statuses


def test_error_details_name_no_internals(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    add_hidden_accounts(session)

    responses = [
        change(bob_client, "POST", "bob"),
        change(bob_client, "POST", "hidden_inactive"),
        change(bob_client, "DELETE", "nonexistent"),
        change(bob_client, "POST", "' OR '1'='1"),
        bob_client.get("/users/alice/followers?cursor=' OR '1'='1"),
        bob_client.get("/users/alice/following?limit=abc"),
    ]

    for response in responses:
        text = response.text.lower()
        for internal in ("sql", "select", "traceback", "follows", "constraint", "ck_"):
            assert internal not in text


def test_rejected_input_is_not_echoed(
    bob_client: TestClient, alice_account: User
) -> None:
    responses = [
        bob_client.get("/users/alice/followers?cursor=something-the-client-sent"),
        bob_client.get("/users/alice/following?limit=something-the-client-sent"),
        change(bob_client, "POST", "something-the-client-sent"),
    ]

    assert [response.status_code for response in responses] == [400, 422, 404]
    for response in responses:
        assert "something" not in response.text


def test_self_follow_is_a_client_error_never_a_server_error(
    make_client, session: Session, bob_account: User
) -> None:
    # The client that would surface a database error as a 500 if one got out.
    client = make_client(raise_server_exceptions=False)
    log_in(client, "bob")

    responses = [change(client, "POST", name) for name in ("bob", "BOB", "bob")]

    assert [response.status_code for response in responses] == [400, 400, 400]
    assert rows(session) == set()


def test_unexpected_error_reveals_nothing_and_leaves_nothing_behind(
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    if method == "DELETE":
        add_follow(session, bob_account, alice_account)
    before = rows(session)
    is_followed_by = users_service._is_followed_by

    def fail_once_the_change_is_made(viewer, user_id):
        # Asked about one user by id, as it is after the write, rather than
        # about the users of a query, as it is when a profile is read.
        if isinstance(user_id, uuid.UUID):
            raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")
        return is_followed_by(viewer, user_id)

    monkeypatch.setattr(
        users_service, "_is_followed_by", fail_once_the_change_is_made
    )
    client = make_client(raise_server_exceptions=False)
    log_in(client, "bob")

    response = change(client, method, "alice")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "postgresql" not in response.text
    # Nothing was committed: a request that fails changes nothing.
    assert rows(session) == before
