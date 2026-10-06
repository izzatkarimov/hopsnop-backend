"""Hopsnop has no public and private accounts.

An account used to carry an ``is_private`` flag that kept its posts to itself.
That is gone: there is no such column, no such field in any request or
response, and nothing that treats one account differently from another
because of it. Following is immediate, and what can be read does not depend
on it.

These tests are about that absence, across the whole backend at once. What
each endpoint does is tested with the endpoint.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.main import app
from app.models import User
from app.schemas.auth import AccountResponse, RegisterRequest
from app.schemas.story import CreateStoryRequest, StoryResponse
from app.schemas.user import (
    FollowResponse,
    MyProfileResponse,
    PublicProfileResponse,
    UpdateProfileRequest,
    UserSummaryResponse,
)
from app.services import posts as posts_service
from app.services import users as users_service
from helpers import (
    PASSWORD,
    columns,
    keys_in,
    log_in,
    recorded_selects,
    registration,
    token_from,
)

APP_SOURCE = Path(__file__).resolve().parent.parent / "app"
# What the removed concept was called, and what a replacement might be.
PRIVACY_WORDS = ("private", "privacy", "visibility", "audience")
EMPTY_UPDATE = "At least one of display_name, bio or avatar_url must be provided."


def register_and_verify(client: TestClient, outbox, **overrides: object):
    """Registers an account through the API and confirms its email address."""
    response = client.post("/auth/register", json=registration(**overrides))
    assert response.status_code == 201, response.text
    token = token_from(outbox.verification[-1][1])
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 200
    return response


# --- the database --------------------------------------------------------


def test_users_table_has_no_privacy_column(session: Session) -> None:
    names = session.scalars(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'users'"
        )
    ).all()

    assert "is_private" not in names
    assert set(names) == {column.name for column in User.__table__.columns}


def test_no_table_has_a_privacy_column(session: Session) -> None:
    names = session.scalars(
        text(
            "SELECT table_name || '.' || column_name "
            "FROM information_schema.columns WHERE table_schema = current_schema()"
        )
    ).all()

    # Not on users, and not moved to posts, follows or stories either.
    assert len(names) > 20
    for name in names:
        for word in PRIVACY_WORDS:
            assert word not in name, name
    # A follow is there or it is not: no state in between, so no requests.
    assert not [name for name in names if "request" in name or "status" in name]


def test_models_have_no_privacy_attribute() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            for word in PRIVACY_WORDS:
                assert word not in column.name, f"{table.name}.{column.name}"

    # Not as a column, and not as a property that stands in for one.
    assert not hasattr(User, "is_private")
    with pytest.raises(TypeError, match="is_private"):
        User(username="carol", is_private=True)


# --- the code and the documented API -------------------------------------


def test_application_code_does_not_mention_the_removed_flag() -> None:
    sources = sorted(APP_SOURCE.rglob("*.py"))

    assert len(sources) > 20
    for source in sources:
        code = source.read_text()
        for leftover in ("is_private", "PrivateAccount", "_posts_readable_by"):
            assert leftover not in code, f"{source.relative_to(APP_SOURCE)}: {leftover}"
    # The refusal that only a private account could cause is gone with it.
    assert not hasattr(posts_service, "PrivateAccountError")


def test_no_schema_has_a_privacy_field() -> None:
    schemas = (
        RegisterRequest,
        AccountResponse,
        UpdateProfileRequest,
        MyProfileResponse,
        PublicProfileResponse,
        UserSummaryResponse,
        FollowResponse,
        CreateStoryRequest,
        StoryResponse,
    )

    for schema in schemas:
        for field in schema.model_fields:
            for word in PRIVACY_WORDS:
                assert word not in field, f"{schema.__name__}.{field}"
    assert "is_private" not in users_service.EDITABLE_FIELDS
    assert set(FollowResponse.model_fields) == {"following"}


def test_documented_api_knows_no_account_privacy() -> None:
    document = app.openapi()
    names = keys_in(document) | {
        parameter["name"]
        for operations in document["paths"].values()
        for operation in operations.values()
        for parameter in operation.get("parameters", [])
    }

    # No property and no parameter of any request or response.
    for name in names:
        for word in PRIVACY_WORDS:
            assert word not in name.lower(), name
    # And nothing the documentation says about one.
    documented = str(document).lower()
    for phrase in ("is_private", "private account", "public account", "privacy"):
        assert phrase not in documented, phrase


# --- registration and the profile ----------------------------------------


@pytest.mark.parametrize("is_private", [True, False])
def test_registration_takes_no_privacy_flag(
    client: TestClient, session: Session, is_private: bool
) -> None:
    response = client.post("/auth/register", json=registration(is_private=is_private))

    # Not read, like any other name registration does not know.
    assert response.status_code == 201
    assert "is_private" not in response.json()
    user = session.scalars(text("SELECT id FROM users")).one()
    assert str(user) == response.json()["id"]
    assert "is_private" not in columns(session, session.get(User, user))


def test_service_refuses_to_write_a_privacy_flag(
    session: Session, alice_account: User
) -> None:
    # The second line of defence, should a caller ever bypass the schema.
    before = columns(session, alice_account)

    with pytest.raises(ValueError, match="Not editable.*is_private"):
        users_service.update_profile(session, alice_account, {"is_private": True})

    assert columns(session, alice_account) == before


def test_own_account_is_shown_without_a_privacy_field(alice_client: TestClient) -> None:
    for path in ("/users/me", "/auth/me", "/users/alice"):
        response = alice_client.get(path)

        assert response.status_code == 200
        assert "is_private" not in response.json()
        assert "private" not in response.text


def test_profile_update_cannot_make_an_account_private(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)
    public_before = make_client().get("/users/alice").json()

    alone = alice_client.patch("/users/me", json={"is_private": True})

    assert alone.status_code == 422
    assert alone.json()["detail"][0]["msg"].endswith(EMPTY_UPDATE)
    assert columns(session, alice_account) == before
    assert make_client().get("/users/alice").json() == public_before


# --- nothing behaves as if an account were private -----------------------


def test_account_that_asks_for_privacy_in_every_way_is_like_any_other(
    make_client, session: Session, bob_account: User, outbox
) -> None:
    anonymous, carol, bob = make_client(), make_client(), make_client()
    register_and_verify(
        anonymous, outbox, username="carol", email="carol@example.com", is_private=True
    )
    assert log_in(carol, "carol").status_code == 200
    assert log_in(bob, "bob").status_code == 200
    carol.patch("/users/me", json={"is_private": True})
    carol.patch("/users/me", json={"is_private": True, "bio": "Keep out"})
    created = carol.post("/posts", json={"content": "Hello", "is_private": True})
    assert created.status_code == 201
    post_id = created.json()["id"]

    # Her profile, her post, her list of posts and the feed, for everyone.
    for viewer in (anonymous, bob, carol):
        assert viewer.get("/users/carol").status_code == 200
        assert viewer.get(f"/posts/{post_id}").json()["content"] == "Hello"
        listed = viewer.get("/users/carol/posts")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [post_id]
        assert [item["id"] for item in viewer.get("/feed").json()["items"]] == [post_id]

    # Liking and reposting it, and replying to it, without following her.
    assert bob.post(f"/posts/{post_id}/like").json() == {
        "liked": True,
        "like_count": 1,
    }
    assert bob.post(f"/posts/{post_id}/repost").json() == {
        "reposted": True,
        "repost_count": 1,
    }
    reply = bob.post("/posts", json={"content": "Hi", "parent_post_id": post_id})
    assert reply.status_code == 201
    seen = anonymous.get(f"/posts/{post_id}").json()
    assert (seen["like_count"], seen["repost_count"]) == (1, 1)

    # Following her at once, with nothing to request and nobody to approve.
    assert bob.post("/users/carol/follow").json() == {"following": True}
    assert anonymous.get("/users/carol").json()["followers_count"] == 1
    followers = anonymous.get("/users/carol/followers").json()["items"]
    assert [item["username"] for item in followers] == ["bob"]
    following = anonymous.get("/users/bob/following").json()["items"]
    assert [item["username"] for item in following] == ["carol"]


def test_no_response_and_no_query_mentions_account_privacy(
    make_client, session: Session, alice_account: User, bob_account: User, outbox
) -> None:
    """Walks through the API and inspects everything that came back."""
    anonymous, alice, bob = make_client(), make_client(), make_client()
    responses = []

    def call(who: TestClient, method: str, path: str, **body: object) -> dict:
        responses.append(who.request(method, path, json=body or None))
        return responses[-1].json() if responses[-1].content else {}

    with recorded_selects() as statements:
        call(
            anonymous,
            "POST",
            "/auth/register",
            **registration(username="carol", email="carol@example.com", is_private=True),
        )
        call(alice, "POST", "/auth/login", identifier="alice", password=PASSWORD)
        call(bob, "POST", "/auth/login", identifier="bob", password=PASSWORD)
        call(alice, "GET", "/auth/me")
        call(alice, "GET", "/auth/sessions")
        call(alice, "GET", "/users/me")
        call(alice, "PATCH", "/users/me", bio="Hello", is_private=True)
        call(alice, "PATCH", "/users/me", is_private=True)  # 422
        post = call(alice, "POST", "/posts", content="Hello Hopsnop!", is_private=True)
        reply = call(bob, "POST", "/posts", content="Hi", parent_post_id=post["id"])
        call(alice, "PATCH", f"/posts/{post['id']}", content="Edited", is_private=True)
        for kind in ("like", "repost"):
            call(bob, "POST", f"/posts/{post['id']}/{kind}")
        call(bob, "POST", "/users/alice/follow")
        for viewer in (anonymous, alice, bob):
            for path in (
                "/users/alice",
                "/users/bob",
                f"/posts/{post['id']}",
                f"/posts/{reply['id']}",
                "/users/alice/posts",
                "/users/alice/posts?is_private=true",
                "/feed",
                "/feed?is_private=true&include_private=true",
                "/users/alice/follow-status",
                "/users/alice/followers",
                "/users/bob/following",
            ):
                call(viewer, "GET", path)
        for kind in ("like", "repost"):
            call(bob, "DELETE", f"/posts/{post['id']}/{kind}")
        call(bob, "DELETE", "/users/alice/follow")
        call(bob, "DELETE", f"/posts/{reply['id']}")  # 204

    # Nothing was refused but the update that had nothing in it to apply.
    statuses = [response.status_code for response in responses]
    assert set(statuses) == {200, 201, 204, 422}
    assert statuses.count(422) == 1
    for response in responses:
        if response.content:
            assert "is_private" not in keys_in(response.json()), response.url
        for word in PRIVACY_WORDS:
            assert word not in response.text.lower(), (response.url, word)
    # The posts, the feed and the lists were all there for all three.
    reads = [response for response in responses if response.request.method == "GET"]
    assert len(reads) >= 33
    assert {response.status_code for response in reads} == {200}
    # And no statement behind any of it looked at such a thing.
    assert len(statements) > 30
    for statement in statements:
        for word in PRIVACY_WORDS:
            assert word not in statement.lower(), statement
