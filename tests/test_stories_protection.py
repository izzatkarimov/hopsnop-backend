"""Security properties of the stories API as a whole.

What the surface is, that the writes are protected like every other write,
what a request can and cannot decide, and what no response may ever contain.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Story, StoryView, User, UserSession
from app.schemas.story import (
    CreateStoryRequest,
    StoryAuthorResponse,
    StoryResponse,
    StoryViewResponse,
)
from app.services import stories as stories_service
from helpers import SENSITIVE_KEYS, add_story, add_user, follow, keys_in, log_in

MEDIA_URL = "https://media.example.com/stories/1.jpg"
FOREIGN_ORIGIN = "https://evil.example"
CROSS_ORIGIN = {"detail": "Cross-origin request rejected."}
# Account, session and row data that must not travel with a story.
NEVER_IN_A_STORY = {
    "email",
    "email_verified_at",
    "is_active",
    "is_private",
    "bio",
    "password_hash",
    "author_id",
    "user_id",
    "viewer_id",
    "viewers",
    "views",
    "viewed_at",
    "session_id",
    "followers_count",
    "following_count",
}


def story_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Story))


def view_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(StoryView))


@pytest.fixture
def story(session: Session, alice_account: User, bob_account: User) -> Story:
    """An active story of alice's, whom bob follows."""
    follow(session, bob_account, alice_account)
    return add_story(session, alice_account, "Hello Hopsnop!")


# --- the surface ---------------------------------------------------------


def test_the_only_story_routes_are_the_intended_ones() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "stor" in path
        for method in operations
    }

    assert routes == {
        ("POST", "/stories"),
        ("GET", "/stories"),
        ("GET", "/stories/{story_id}"),
        ("DELETE", "/stories/{story_id}"),
        ("POST", "/stories/{story_id}/view"),
    }


def test_nothing_beyond_this_phase_is_exposed() -> None:
    document = app.openapi()
    story_paths = " ".join(path for path in document["paths"] if "stor" in path)
    documented = str(
        [document["paths"][path] for path in document["paths"] if "stor" in path]
    ).lower()

    for later in ("viewers", "views", "users", "repl", "react", "upload", "highlight"):
        assert later not in story_paths, later
    # Images by their address, and nothing else.
    for word in ("video", "upload", "multipart", "sticker", "music", "mention"):
        assert word not in documented, word
    for word in ("private", "privacy", "visibility", "audience", "close_friends"):
        assert word not in documented, word


def test_request_schema_holds_only_what_a_client_may_decide() -> None:
    assert set(CreateStoryRequest.model_fields) == {"media_url", "caption"}
    documented = app.openapi()["components"]["schemas"]["CreateStoryRequest"]
    assert set(documented["properties"]) == {"media_url", "caption"}
    assert documented["required"] == ["media_url"]


def test_response_schemas_hold_no_account_data_and_no_viewers() -> None:
    story = set(StoryResponse.model_fields)
    author = set(StoryAuthorResponse.model_fields)

    assert story == {
        "id",
        "author",
        "media_url",
        "media_type",
        "caption",
        "created_at",
        "expires_at",
        "viewed_by_me",
        "view_count",
    }
    assert author == {"username", "display_name", "avatar_url"}
    assert set(StoryViewResponse.model_fields) == {"viewed"}
    assert (story | author).isdisjoint(SENSITIVE_KEYS | NEVER_IN_A_STORY)
    schemas = app.openapi()["components"]["schemas"]
    assert set(schemas["StoryResponse"]["properties"]) == story
    assert set(schemas["StoryAuthorResponse"]["properties"]) == author


def test_story_requests_take_the_story_from_the_path_and_nothing_else() -> None:
    paths = app.openapi()["paths"]

    for path in ("/stories/{story_id}", "/stories/{story_id}/view"):
        for operation in paths[path].values():
            assert [param["name"] for param in operation["parameters"]] == ["story_id"]
    assert "requestBody" not in paths["/stories/{story_id}/view"]["post"]
    assert "requestBody" not in paths["/stories/{story_id}"]["delete"]


def test_media_type_is_the_servers_and_only_ever_an_image(
    alice_client: TestClient, session: Session
) -> None:
    assert stories_service.MEDIA_TYPE == "image"

    for claimed in ("video", "image/png", "gif", None, 5):
        response = alice_client.post(
            "/stories", json={"media_url": MEDIA_URL, "media_type": claimed}
        )
        assert response.status_code == 201
        assert response.json()["media_type"] == "image"
    assert set(session.scalars(select(Story.media_type))) == {"image"}


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_story_cannot_be_changed_once_published(
    alice_client: TestClient, session: Session, story: Story, method: str
) -> None:
    response = alice_client.request(
        method,
        f"/stories/{story.id}",
        json={"caption": "Edited", "expires_at": "2099-01-01T00:00:00Z"},
    )

    assert response.status_code == 405
    session.refresh(story)
    assert story.caption == "Hello Hopsnop!"
    assert story.expires_at == story.created_at + timedelta(hours=24)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_collection_accepts_no_other_write_methods(
    alice_client: TestClient, session: Session, story: Story, method: str
) -> None:
    response = alice_client.request(method, "/stories", json={"media_url": MEDIA_URL})

    assert response.status_code == 405
    assert story_count(session) == 1


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_view_can_only_be_recorded_never_read_or_taken_back(
    bob_client: TestClient, session: Session, story: Story, method: str
) -> None:
    assert bob_client.post(f"/stories/{story.id}/view").status_code == 200

    response = bob_client.request(method, f"/stories/{story.id}/view")

    assert response.status_code == 405
    assert view_count(session) == 1


# --- CSRF ----------------------------------------------------------------


def test_cross_site_story_creation_is_rejected_and_has_no_effect(
    alice_client: TestClient, session: Session
) -> None:
    response = alice_client.post(
        "/stories",
        json={"media_url": MEDIA_URL, "caption": "Posted by another website"},
        headers={"Origin": FOREIGN_ORIGIN},
    )

    assert response.status_code == 403
    assert response.json() == CROSS_ORIGIN
    assert story_count(session) == 0


@pytest.mark.parametrize("header", ["Origin", "Referer"])
def test_cross_site_view_and_deletion_are_rejected_and_have_no_effect(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    story: Story,
    header: str,
) -> None:
    source = FOREIGN_ORIGIN if header == "Origin" else f"{FOREIGN_ORIGIN}/page"

    answers = [
        bob_client.post(f"/stories/{story.id}/view", headers={header: source}),
        alice_client.delete(f"/stories/{story.id}", headers={header: source}),
    ]

    for response in answers:
        assert response.status_code == 403
        assert response.json() == CROSS_ORIGIN
    assert (story_count(session), view_count(session)) == (1, 0)


def test_cross_site_attempt_is_refused_before_the_story_is_looked_at(
    bob_client: TestClient, session: Session, story: Story
) -> None:
    answers = [
        bob_client.post(
            f"/stories/{story_id}/view", headers={"Origin": FOREIGN_ORIGIN}
        )
        for story_id in (story.id, uuid.uuid4())
    ]

    assert [response.status_code for response in answers] == [403, 403]
    assert answers[0].text == answers[1].text


def test_requests_from_the_frontend_are_let_through(
    alice_client: TestClient, bob_client: TestClient, story: Story
) -> None:
    frontend = {"Origin": "http://localhost:3000"}

    created = alice_client.post(
        "/stories", json={"media_url": MEDIA_URL}, headers=frontend
    )
    viewed = bob_client.post(f"/stories/{story.id}/view", headers=frontend)
    deleted = alice_client.delete(f"/stories/{story.id}", headers=frontend)

    assert [r.status_code for r in (created, viewed, deleted)] == [201, 200, 204]
    assert created.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cross_site_read_gets_no_cors_permission(
    bob_client: TestClient, story: Story
) -> None:
    for path in ("/stories", f"/stories/{story.id}"):
        response = bob_client.get(path, headers={"Origin": FOREIGN_ORIGIN})

        # The browser will not let the other site's page read this.
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


def test_story_writes_need_a_json_body_where_they_take_one(
    alice_client: TestClient, session: Session
) -> None:
    # A cross-site HTML form can send text without a preflight, but only
    # with a form content type.
    response = alice_client.post(
        "/stories",
        content=f'{{"media_url": "{MEDIA_URL}"}}',
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert story_count(session) == 0


# --- accounts that may no longer act -------------------------------------


def test_password_reset_ends_the_sessions_that_could_read_stories(
    bob_client: TestClient, make_client, story: Story, outbox
) -> None:
    from helpers import token_from

    assert bob_client.get(f"/stories/{story.id}").status_code == 200
    anonymous = make_client()
    anonymous.post("/auth/forgot-password", json={"email": "bob@example.com"})
    token = token_from(outbox.password_reset[0][1])
    reset = anonymous.post(
        "/auth/reset-password",
        json={"token": token, "new_password": "a completely new passphrase"},
    )
    assert reset.status_code == 200

    assert bob_client.get(f"/stories/{story.id}").status_code == 401
    assert bob_client.get("/stories").status_code == 401


def test_deactivated_followers_view_is_no_longer_possible(
    bob_client: TestClient, session: Session, story: Story, bob_account: User
) -> None:
    bob_account.is_active = False
    session.flush()

    assert bob_client.post(f"/stories/{story.id}/view").status_code == 401
    assert view_count(session) == 0


# --- what responses may contain ------------------------------------------


def test_no_story_response_contains_a_secret_or_a_viewer(
    make_client, session: Session, alice_account: User, bob_account: User, clock
) -> None:
    """Walks through every story endpoint and inspects everything that came back."""
    alice, bob, anonymous = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    carol = add_user(session, "carol")
    follow(session, bob_account, alice_account)
    hidden = add_story(session, carol, "Hidden from everyone here")
    expired = add_story(
        session,
        alice_account,
        "Hidden: expired",
        created_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    responses = []

    def call(who: TestClient, method: str, path: str, **kwargs: object) -> dict:
        responses.append(who.request(method, path, **kwargs))
        return responses[-1].json() if responses[-1].content else {}

    story = call(alice, "POST", "/stories", json={"media_url": MEDIA_URL, "caption": "Hi"})
    own = f"/stories/{story['id']}"
    call(alice, "POST", "/stories", json={"media_url": "not a url"})  # 422
    call(alice, "POST", "/stories", json={"caption": "x" * 151})  # 422
    call(anonymous, "POST", "/stories", json={"media_url": MEDIA_URL})  # 401
    call(alice, "POST", "/stories", json={}, headers={"Origin": FOREIGN_ORIGIN})  # 403
    for viewer in (alice, bob, anonymous):
        call(viewer, "GET", own)
        call(viewer, "GET", "/stories")
        call(viewer, "GET", "/stories?limit=1")
        call(viewer, "GET", f"/stories/{hidden.id}")  # 404, 404, 401
        call(viewer, "GET", f"/stories/{expired.id}")  # 404, 404, 401
        call(viewer, "POST", f"{own}/view")
        call(viewer, "POST", f"{own}/view")  # again
        call(viewer, "POST", f"/stories/{hidden.id}/view")
    call(bob, "GET", "/stories/not-an-id")  # 422
    call(bob, "GET", "/stories?cursor=abc")  # 400
    call(bob, "GET", "/stories?limit=0")  # 422
    # A secret sent in is not sent back either.
    call(bob, "GET", f"/stories/{bob.cookies.get('__Host-hopsnop_session')}")  # 422
    call(bob, "PATCH", own, json={"caption": "Hacked"})  # 405
    call(bob, "DELETE", own)  # 403
    call(bob, "DELETE", f"/stories/{hidden.id}")  # 404
    call(alice, "GET", own)
    call(alice, "DELETE", own)  # 204
    call(alice, "DELETE", own)  # 404

    seen = {response.status_code for response in responses}
    assert seen == {200, 201, 204, 400, 401, 403, 404, 405, 422}

    session_tokens = [client.cookies.get("__Host-hopsnop_session") for client in (alice, bob)]
    token_hashes = session.scalars(select(UserSession.token_hash)).all()
    session_ids = [str(id) for id in session.scalars(select(UserSession.id))]
    secrets = [
        *session_tokens,
        *token_hashes,
        *session_ids,
        alice_account.password_hash,
        alice_account.email,
        bob_account.email,
        # Who viewed a story, by any name.
        str(bob_account.id),
        str(alice_account.id),
    ]
    assert len(token_hashes) == 2

    for response in responses:
        if response.content:
            assert keys_in(response.json()).isdisjoint(
                SENSITIVE_KEYS | NEVER_IN_A_STORY
            ), response.url
        for secret in secrets:
            assert secret not in response.text, response.url
        assert "set-cookie" not in response.headers, response.url
        for name, value in response.headers.items():
            for secret in secrets:
                assert secret not in value, (response.url, name)
        # What is hidden appears in no response, whoever asked.
        assert "Hidden" not in response.text, response.url
        assert "carol" not in response.text, response.url
        for word in ("private", "privacy", "visibility"):
            assert word not in response.text.lower(), response.url


# --- errors --------------------------------------------------------------


def test_every_story_error_has_the_same_shape(
    alice_client: TestClient, bob_client: TestClient, make_client, story: Story
) -> None:
    errors = [
        alice_client.post("/stories", json={}),  # 422
        make_client().get("/stories"),  # 401
        bob_client.delete(f"/stories/{story.id}"),  # 403
        bob_client.get(f"/stories/{uuid.uuid4()}"),  # 404
        bob_client.get("/stories?cursor=abc"),  # 400
        bob_client.put(f"/stories/{story.id}"),  # 405
        bob_client.post(
            f"/stories/{story.id}/view", headers={"Origin": FOREIGN_ORIGIN}
        ),  # 403
    ]

    statuses = [response.status_code for response in errors]
    assert statuses == [422, 401, 403, 404, 400, 405, 403]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"
    # Each failure a client can act on differently says which one it is, and
    # none of them is a conflict: nothing about repeating a request is one.
    details = [str(response.json()["detail"]) for response in errors[1:]]
    assert len(set(details)) == len(details)
    assert 409 not in statuses


def test_error_details_name_no_internals(
    alice_client: TestClient, bob_client: TestClient, story: Story
) -> None:
    responses = [
        bob_client.delete(f"/stories/{story.id}"),
        bob_client.get(f"/stories/{uuid.uuid4()}"),
        bob_client.post(f"/stories/{uuid.uuid4()}/view"),
        bob_client.get("/stories?cursor=' OR '1'='1"),
        bob_client.get("/stories/' OR '1'='1"),
        alice_client.post("/stories", json={"media_url": "javascript:alert(1)"}),
    ]

    for response in responses:
        text = response.text.lower()
        for internal in ("sql", "select", "traceback", "story_views", "constraint"):
            assert internal not in text


def test_validation_error_does_not_echo_what_was_sent(
    alice_client: TestClient,
) -> None:
    response = alice_client.post(
        "/stories",
        json={"media_url": "something-the-client-sent", "caption": "y" * 200},
    )

    assert response.status_code == 422
    assert "something" not in response.text
    assert "yyyy" not in response.text
    for error in response.json()["detail"]:
        assert set(error) == {"loc", "msg", "type"}


@pytest.mark.parametrize("operation", ["create_story", "record_view", "delete_story"])
def test_unexpected_error_reveals_nothing_and_leaves_nothing_behind(
    make_client,
    session: Session,
    story: Story,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    story_id = story.id
    real = stories_service.insert if operation == "record_view" else None

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")

    def fail_after_writing(*args: object, **kwargs: object):
        # The view is written, and then the request fails before it commits.
        monkeypatch.setattr(stories_service, "exists", fail)
        return real(*args, **kwargs)

    if operation == "record_view":
        monkeypatch.setattr(stories_service, "insert", fail_after_writing)
    else:
        monkeypatch.setattr(stories_service, operation, fail)
    client = make_client(raise_server_exceptions=False)
    log_in(client, "bob" if operation == "record_view" else "alice")

    response = {
        "create_story": lambda: client.post("/stories", json={"media_url": MEDIA_URL}),
        "record_view": lambda: client.post(f"/stories/{story_id}/view"),
        "delete_story": lambda: client.delete(f"/stories/{story_id}"),
    }[operation]()

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "postgresql" not in response.text
    # Nothing was committed: a request that fails changes nothing.
    assert (story_count(session), view_count(session)) == (1, 0)
