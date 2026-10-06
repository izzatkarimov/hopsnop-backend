"""Security properties of the posts API as a whole.

Who may change what across users, what the endpoints give away about posts
that are hidden from the caller, and what no response may ever contain.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Post, User, UserSession
from app.schemas.post import (
    CreatePostRequest,
    PostAuthorResponse,
    PostResponse,
    UpdatePostRequest,
)
from app.services import posts as posts_service
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    keys_in,
    log_in,
    post_columns,
    token_from,
)

FOREIGN_ORIGIN = "https://evil.example"
NOT_FOUND = {"detail": "Post not found."}
NOT_THE_AUTHOR = {"detail": "You are not the author of this post."}
# Account data that must not travel with a post.
NEVER_IN_A_POST = {
    "email",
    "email_verified_at",
    "is_active",
    "password_hash",
    "author_id",
    "deleted_at",
}


def all_posts(session: Session) -> dict[uuid.UUID, dict[str, object]]:
    """Every row of the posts table, as currently stored."""
    return {
        id: post_columns(session, id) for id in session.scalars(select(Post.id)).all()
    }


# --- the surface ---------------------------------------------------------


def test_the_only_post_routes_are_the_intended_ones() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "post" in path
        for method in operations
    }

    assert routes == {
        ("POST", "/posts"),
        ("GET", "/posts/{post_id}"),
        ("PATCH", "/posts/{post_id}"),
        ("DELETE", "/posts/{post_id}"),
        ("POST", "/posts/{post_id}/like"),
        ("DELETE", "/posts/{post_id}/like"),
        ("POST", "/posts/{post_id}/repost"),
        ("DELETE", "/posts/{post_id}/repost"),
        ("GET", "/users/{username}/posts"),
    }


def test_nothing_from_later_phases_is_exposed() -> None:
    paths = " ".join(app.openapi()["paths"])

    later_phases = ("upload", "media")
    for later in (*later_phases, "search", "notif", "mention", "hashtag"):
        assert later not in paths


def test_request_schemas_hold_only_what_a_client_may_decide() -> None:
    assert set(CreatePostRequest.model_fields) == {"content", "parent_post_id"}
    assert set(UpdatePostRequest.model_fields) == {"content"}


def test_response_schemas_hold_no_account_data() -> None:
    post = set(PostResponse.model_fields) | set(PostResponse.model_computed_fields)
    author = set(PostAuthorResponse.model_fields)

    assert post == {
        "id",
        "author",
        "content",
        "parent_post_id",
        "created_at",
        "updated_at",
        "is_reply",
        "like_count",
        "liked_by_me",
        "repost_count",
        "reposted_by_me",
    }
    assert author == {"id", "username", "display_name", "avatar_url"}
    assert (post | author).isdisjoint(SENSITIVE_KEYS | NEVER_IN_A_POST)


def test_documented_responses_match_the_schemas() -> None:
    schemas = app.openapi()["components"]["schemas"]

    assert set(schemas["PostResponse"]["properties"]) == set(
        PostResponse.model_fields
    ) | {"is_reply"}
    assert set(schemas["PostAuthorResponse"]["properties"]) == set(
        PostAuthorResponse.model_fields
    )
    assert set(schemas["CreatePostRequest"]["properties"]) == {
        "content",
        "parent_post_id",
    }
    assert set(schemas["UpdatePostRequest"]["properties"]) == {"content"}


@pytest.mark.parametrize("method", ["PUT", "POST"])
def test_single_post_accepts_no_other_write_methods(
    alice_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    post = add_post(session, alice_account)
    before = all_posts(session)

    response = alice_client.request(method, f"/posts/{post.id}", json={"content": "x"})

    assert response.status_code == 405
    assert all_posts(session) == before


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_collection_only_accepts_post(
    alice_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    # In particular there is no listing here. Everybody's posts are listed by
    # the feed, under its own rules.
    add_post(session, alice_account)
    before = all_posts(session)

    response = alice_client.request(method, "/posts", json={"content": "x"})

    assert response.status_code == 405
    assert all_posts(session) == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_users_posts_can_only_be_read_through_their_listing(
    alice_client: TestClient, session: Session, bob_account: User, method: str
) -> None:
    add_post(session, bob_account)
    before = all_posts(session)

    response = alice_client.request(
        method, "/users/bob/posts", json={"content": "Planted on bob's page"}
    )

    assert response.status_code == 405
    assert all_posts(session) == before


# --- changing another user's posts ---------------------------------------


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_another_users_post_cannot_be_changed(
    alice_client: TestClient, session: Session, bob_account: User, method: str
) -> None:
    post = add_post(session, bob_account, "Bob's words")
    before = all_posts(session)

    response = alice_client.request(
        method, f"/posts/{post.id}", json={"content": "Hacked"}
    )

    assert response.status_code == 403
    assert response.json() == NOT_THE_AUTHOR
    assert all_posts(session) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_hidden_post_of_another_user_cannot_be_changed_or_even_confirmed(
    alice_client: TestClient, session: Session, method: str
) -> None:
    gone = add_user(session, "gone", active=False)
    post = add_post(session, gone, "Not shown to anyone")
    before = all_posts(session)

    hidden = alice_client.request(
        method, f"/posts/{post.id}", json={"content": "Hacked"}
    )
    missing = alice_client.request(
        method, f"/posts/{uuid.uuid4()}", json={"content": "Hacked"}
    )

    # "Forbidden" would confirm that the id belongs to a post. A post the
    # caller cannot see is answered exactly like one that does not exist.
    assert hidden.status_code == missing.status_code == 404
    assert hidden.text == missing.text
    assert sorted(hidden.headers) == sorted(missing.headers)
    assert all_posts(session) == before


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_write_attempts_cannot_be_used_to_probe_for_hidden_posts(
    alice_client: TestClient, session: Session, bob_account: User, method: str
) -> None:
    deleted = add_post(session, bob_account, deleted=True)
    inactive = add_post(session, add_user(session, "inactive", active=False))
    unverified = add_post(session, add_user(session, "unverified", verified=False))

    responses = [
        alice_client.request(method, f"/posts/{id}", json={"content": "Probe"})
        for id in (uuid.uuid4(), deleted.id, inactive.id, unverified.id)
    ]

    assert {response.status_code for response in responses} == {404}
    assert len({response.text for response in responses}) == 1


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_target_of_a_change_is_only_ever_the_post_in_the_path(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    own = add_post(session, alice_account, "Alice's")
    bobs = add_post(session, bob_account, "Bob's")
    bobs_before = post_columns(session, bobs.id)

    # Every way a client might try to point the change at another post.
    response = alice_client.request(
        method,
        f"/posts/{own.id}?post_id={bobs.id}&id={bobs.id}",
        json={"content": "Edited", "id": str(bobs.id), "post_id": str(bobs.id)},
    )

    assert response.status_code in (200, 204)
    assert post_columns(session, bobs.id) == bobs_before


def test_ownership_cannot_be_claimed_in_the_request(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, bob_account, "Bob's words")
    before = all_posts(session)

    edited = alice_client.patch(
        f"/posts/{post.id}?author_id={alice_account.id}&user_id={bob_account.id}",
        json={
            "content": "Hacked",
            "author_id": str(alice_account.id),
            "user_id": str(bob_account.id),
            "is_author": True,
        },
        headers={"X-User-Id": str(bob_account.id)},
    )

    assert edited.status_code == 403
    assert all_posts(session) == before


def test_users_can_only_change_their_own_posts_in_both_directions(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    alices = add_post(session, alice_account, "Alice's")
    bobs = add_post(session, bob_account, "Bob's")
    before = all_posts(session)

    attempts = [
        alice_client.patch(f"/posts/{bobs.id}", json={"content": "Hacked"}),
        alice_client.delete(f"/posts/{bobs.id}"),
        bob_client.patch(f"/posts/{alices.id}", json={"content": "Hacked"}),
        bob_client.delete(f"/posts/{alices.id}"),
    ]

    assert [response.status_code for response in attempts] == [403] * 4
    assert all_posts(session) == before

    own_edit = alice_client.patch(f"/posts/{alices.id}", json={"content": "Mine"})
    assert own_edit.status_code == 200
    assert bob_client.delete(f"/posts/{bobs.id}").status_code == 204


def test_refusal_does_not_depend_on_the_text_being_valid_for_hidden_posts(
    alice_client: TestClient, session: Session
) -> None:
    hidden = add_post(session, add_user(session, "gone", active=False))

    # An invalid text is rejected for what it is, whatever the id; a valid
    # one gets "not found". Neither tells a hidden post from no post.
    invalid = [
        alice_client.patch(f"/posts/{id}", json={"content": ""})
        for id in (hidden.id, uuid.uuid4())
    ]
    valid = [
        alice_client.patch(f"/posts/{id}", json={"content": "x"})
        for id in (hidden.id, uuid.uuid4())
    ]

    assert [response.status_code for response in invalid] == [422, 422]
    assert invalid[0].text == invalid[1].text
    assert [response.status_code for response in valid] == [404, 404]
    assert valid[0].text == valid[1].text


# --- CSRF ----------------------------------------------------------------


def test_cross_site_post_creation_is_rejected_and_has_no_effect(
    alice_client: TestClient, session: Session
) -> None:
    response = alice_client.post(
        "/posts",
        json={"content": "Posted by another website"},
        headers={"Origin": FOREIGN_ORIGIN},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request rejected."}
    assert all_posts(session) == {}


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_cross_site_change_is_rejected_and_has_no_effect(
    alice_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    post = add_post(session, alice_account, "Original")
    before = all_posts(session)

    response = alice_client.request(
        method,
        f"/posts/{post.id}",
        json={"content": "Changed by another website"},
        headers={"Origin": FOREIGN_ORIGIN},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request rejected."}
    assert all_posts(session) == before


def test_cross_site_read_gets_no_cors_permission(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)

    response = alice_client.get(f"/posts/{post.id}", headers={"Origin": FOREIGN_ORIGIN})

    # The browser will not let the other site's page read this.
    assert "access-control-allow-origin" not in response.headers


def test_reading_never_changes_a_post(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account)
    add_post(session, bob_account, "Reply", parent=post)
    before = all_posts(session)
    bob = make_client()
    log_in(bob, "bob")

    for viewer in (make_client(), bob):
        viewer.get(f"/posts/{post.id}")
        viewer.get(f"/posts/{uuid.uuid4()}")
        viewer.get("/users/alice/posts")
        viewer.get("/users/alice/posts?limit=1")

    assert all_posts(session) == before


# --- accounts that may no longer act -------------------------------------


def test_deactivated_user_can_do_nothing_with_posts(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account, "Written while active")
    alice_account.is_active = False
    session.flush()
    before = all_posts(session)

    attempts = [
        alice_client.post("/posts", json={"content": "Still here"}),
        alice_client.patch(f"/posts/{post.id}", json={"content": "Still here"}),
        alice_client.delete(f"/posts/{post.id}"),
    ]

    assert [response.status_code for response in attempts] == [401, 401, 401]
    assert all_posts(session) == before
    # And what they wrote is no longer shown, to them or to anyone.
    for viewer in (alice_client, make_client()):
        assert viewer.get(f"/posts/{post.id}").status_code == 404
        assert viewer.get("/users/alice/posts").status_code == 404


def test_posts_reappear_when_an_account_is_reactivated(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # Nothing was destroyed by the deactivation.
    post = add_post(session, alice_account)
    alice_account.is_active = False
    session.flush()
    assert client.get(f"/posts/{post.id}").status_code == 404

    alice_account.is_active = True
    session.flush()

    assert client.get(f"/posts/{post.id}").status_code == 200


def test_password_reset_ends_the_sessions_that_could_post(
    alice_client: TestClient, session: Session, outbox
) -> None:
    assert alice_client.post("/posts", json={"content": "Before"}).status_code == 201
    alice_client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    alice_client.post(
        "/auth/reset-password",
        json={
            "token": token_from(outbox.password_reset[0][1]),
            "new_password": "a brand new passphrase",
        },
    )

    assert alice_client.post("/posts", json={"content": "After"}).status_code == 401
    assert session.scalar(select(func.count()).select_from(Post)) == 1


# --- what responses may contain ------------------------------------------


def test_no_post_response_contains_a_secret(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    """Walks through every post endpoint and inspects everything that came back."""
    alice, bob, anonymous = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    gone = add_user(session, "gone", active=False)
    hidden = add_post(session, gone, "Hidden from everyone")
    old = add_post(
        session,
        alice_account,
        "Too old to edit",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    responses = []

    def call(who: TestClient, method: str, path: str, **body: object) -> dict:
        responses.append(who.request(method, path, json=body or None))
        return responses[-1].json() if responses[-1].content else {}

    post = call(alice, "POST", "/posts", content="Hello Hopsnop!")
    reply = call(alice, "POST", "/posts", content="Reply", parent_post_id=post["id"])
    call(alice, "POST", "/posts", content="")  # 422
    call(alice, "POST", "/posts", content="x", parent_post_id=str(hidden.id))  # 404
    call(anonymous, "POST", "/posts", content="Hello")  # 401
    call(alice, "GET", f"/posts/{post['id']}")
    call(anonymous, "GET", f"/posts/{post['id']}")
    call(bob, "GET", f"/posts/{post['id']}")
    call(alice, "GET", f"/posts/{hidden.id}")  # 404
    call(anonymous, "GET", f"/posts/{hidden.id}")  # 404
    call(alice, "GET", "/posts/not-an-id")  # 422
    call(alice, "PATCH", f"/posts/{post['id']}", content="Edited")
    call(alice, "PATCH", f"/posts/{old.id}", content="Too late")  # 409
    call(bob, "PATCH", f"/posts/{post['id']}", content="Hacked")  # 403
    call(anonymous, "PATCH", f"/posts/{post['id']}", content="Hacked")  # 401
    call(alice, "GET", "/users/alice/posts")
    call(anonymous, "GET", "/users/alice/posts?limit=1")
    call(alice, "GET", "/users/bob/posts")
    call(bob, "GET", "/users/bob/posts")
    call(alice, "GET", "/users/gone/posts")  # 404
    call(alice, "GET", "/users/nonexistent/posts")  # 404
    call(alice, "GET", "/users/alice/posts?cursor=abc")  # 400
    call(alice, "GET", "/users/alice/posts?limit=0")  # 422
    call(bob, "DELETE", f"/posts/{reply['id']}")  # 403
    call(alice, "DELETE", f"/posts/{reply['id']}")  # 204
    call(alice, "DELETE", f"/posts/{reply['id']}")  # 404

    seen = {response.status_code for response in responses}
    assert seen == {200, 201, 204, 400, 401, 403, 404, 409, 422}

    session_tokens = [
        client.cookies.get("hopsnop_session") for client in (alice, bob)
    ]
    token_hashes = session.scalars(select(UserSession.token_hash)).all()
    session_ids = [str(id) for id in session.scalars(select(UserSession.id))]
    secrets = [
        *session_tokens,
        *token_hashes,
        *session_ids,
        alice_account.password_hash,
        alice_account.email,
        bob_account.email,
    ]
    assert len(token_hashes) == 2

    for response in responses:
        if response.content:
            assert keys_in(response.json()).isdisjoint(
                SENSITIVE_KEYS | NEVER_IN_A_POST
            ), response.url
        for secret in secrets:
            assert secret not in response.text, response.url
        assert "set-cookie" not in response.headers, response.url
        for name, value in response.headers.items():
            for secret in secrets:
                assert secret not in value, (response.url, name)
    # What is not shown appears in no response, whoever asked.
    for response in responses:
        assert "Hidden from everyone" not in response.text, response.url


def test_every_post_error_has_the_same_shape(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    bobs = add_post(session, bob_account)
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    old = add_post(session, alice_account, created_at=two_hours_ago)

    errors = [
        alice_client.post("/posts", json={}),  # 422
        make_client().post("/posts", json={"content": "x"}),  # 401
        alice_client.patch(f"/posts/{bobs.id}", json={"content": "x"}),  # 403
        alice_client.get(f"/posts/{uuid.uuid4()}"),  # 404
        alice_client.patch(f"/posts/{old.id}", json={"content": "x"}),  # 409
        alice_client.get("/users/alice/posts?cursor=abc"),  # 400
        alice_client.get("/users/nonexistent/posts"),  # 404
        alice_client.post(
            "/posts", json={"content": "x", "parent_post_id": str(uuid.uuid4())}
        ),  # 404
        alice_client.post(
            "/posts", json={"content": "x"}, headers={"Origin": FOREIGN_ORIGIN}
        ),  # 403
    ]

    statuses = [response.status_code for response in errors]
    assert statuses == [422, 401, 403, 404, 409, 400, 404, 404, 403]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"
    # Each failure a client can act on differently says which one it is.
    details = [str(response.json()["detail"]) for response in errors[1:]]
    assert len(set(details)) == len(details)


def test_error_details_name_no_internals(
    alice_client: TestClient, session: Session, bob_account: User
) -> None:
    post = add_post(session, bob_account)

    responses = [
        alice_client.patch(f"/posts/{post.id}", json={"content": "x"}),
        alice_client.get(f"/posts/{uuid.uuid4()}"),
        alice_client.post(
            "/posts", json={"content": "x", "parent_post_id": str(uuid.uuid4())}
        ),
        alice_client.get("/users/alice/posts?cursor=abc"),
    ]

    for response in responses:
        text = response.text.lower()
        for internal in ("sql", "select", "traceback", "posts.", "constraint", "uuid("):
            assert internal not in text


def test_unexpected_error_reveals_nothing_about_itself(
    make_client, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")

    monkeypatch.setattr(posts_service, "create_post", fail)
    client = make_client(raise_server_exceptions=False)
    log_in(client, "alice")

    response = client.post("/posts", json={"content": "Hello Hopsnop!"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "postgresql" not in response.text
