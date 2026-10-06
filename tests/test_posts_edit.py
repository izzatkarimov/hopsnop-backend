"""Editing posts: only the author, only the text, only for 60 minutes."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.post import UpdatePostRequest
from app.services import posts as posts_service
from helpers import add_post, post_columns, recorded_selects

NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_FOUND = {"detail": "Post not found."}
NOT_THE_AUTHOR = {"detail": "You are not the author of this post."}
TOO_LATE = {
    "detail": "A post can only be edited within 60 minutes of being created."
}


def create(client: TestClient, content: str = "Original", **extra: object) -> dict:
    response = client.post("/posts", json={"content": content, **extra})
    assert response.status_code == 201
    return response.json()


def edit(
    client: TestClient, post_id: object, content: object = "Edited", **extra: object
):
    return client.patch(f"/posts/{post_id}", json={"content": content, **extra})


def minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


# --- editing one's own post ----------------------------------------------


def test_author_can_edit_their_post(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")

    response = edit(alice_client, post["id"], "Edited")

    assert response.status_code == 200
    assert response.json()["content"] == "Edited"
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Edited"


def test_edit_returns_the_post_in_its_usual_shape(alice_client: TestClient) -> None:
    post = create(alice_client)

    edited = edit(alice_client, post["id"]).json()

    assert set(edited) == set(post)
    assert edited["id"] == post["id"]
    assert edited["author"] == post["author"]
    assert edited["parent_post_id"] is None
    assert edited["is_reply"] is False


def test_edited_post_is_what_readers_see(
    alice_client: TestClient, make_client
) -> None:
    post = create(alice_client, "Original")

    edited = edit(alice_client, post["id"], "Edited").json()

    assert make_client().get(f"/posts/{post['id']}").json() == edited


def test_edit_changes_updated_at_but_not_created_at(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client)
    created_at = clock.now

    clock.advance(minutes=10)
    edited = edit(alice_client, post["id"]).json()

    assert edited["created_at"] == post["created_at"]
    assert datetime.fromisoformat(edited["updated_at"]) == created_at + timedelta(
        minutes=10
    )
    stored = post_columns(session, uuid.UUID(post["id"]))
    assert stored["created_at"] == created_at
    assert stored["updated_at"] == created_at + timedelta(minutes=10)


def test_each_edit_moves_updated_at(alice_client: TestClient, clock) -> None:
    post = create(alice_client)
    seen = [post["updated_at"]]

    for number in range(3):
        clock.advance(minutes=5)
        edited = edit(alice_client, post["id"], f"Edit {number}").json()
        seen.append(edited["updated_at"])

    assert seen == sorted(set(seen))


def test_saving_the_same_text_is_not_an_edit(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client, "Original")

    clock.advance(minutes=10)
    response = edit(alice_client, post["id"], "Original")

    assert response.status_code == 200
    # Nothing changed, so the post must not start to look edited.
    assert response.json()["updated_at"] == post["updated_at"]
    stored = post_columns(session, uuid.UUID(post["id"]))
    assert stored["updated_at"] == stored["created_at"]


def test_edit_only_changes_the_text(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client)
    before = post_columns(session, uuid.UUID(post["id"]))

    clock.advance(minutes=1)
    edit(alice_client, post["id"])

    after = post_columns(session, uuid.UUID(post["id"]))
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"content", "updated_at"}


def test_edit_only_changes_the_post_it_names(
    alice_client: TestClient, session: Session
) -> None:
    first, second = create(alice_client, "First"), create(alice_client, "Second")
    before = post_columns(session, uuid.UUID(second["id"]))

    edit(alice_client, first["id"], "Edited")

    assert post_columns(session, uuid.UUID(second["id"])) == before


def test_owner_of_a_private_account_can_edit_their_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_private = True
    session.flush()
    post = create(alice_client)

    assert edit(alice_client, post["id"]).status_code == 200


def test_edit_responses_are_not_to_be_cached(alice_client: TestClient) -> None:
    post = create(alice_client)

    assert edit(alice_client, post["id"]).headers["cache-control"] == "no-store"


# --- the 60-minute window ------------------------------------------------


@pytest.mark.parametrize(
    "age",
    [
        timedelta(0),
        timedelta(minutes=1),
        timedelta(minutes=30),
        timedelta(minutes=59),
        timedelta(minutes=59, seconds=59),
        timedelta(minutes=59, seconds=59, microseconds=999_999),
    ],
)
def test_post_can_be_edited_until_it_is_60_minutes_old(
    alice_client: TestClient, clock, age: timedelta
) -> None:
    post = create(alice_client)

    clock.advance(seconds=age.total_seconds())
    response = edit(alice_client, post["id"])

    assert response.status_code == 200
    assert response.json()["content"] == "Edited"


@pytest.mark.parametrize(
    "age",
    [
        # At the deadline itself the window is closed.
        timedelta(minutes=60),
        timedelta(minutes=60, microseconds=1),
        timedelta(minutes=60, seconds=1),
        timedelta(minutes=61),
        timedelta(hours=2),
        timedelta(days=1),
        timedelta(days=365),
    ],
)
def test_post_cannot_be_edited_once_it_is_60_minutes_old(
    alice_client: TestClient, session: Session, clock, age: timedelta
) -> None:
    post = create(alice_client, "Original")
    before = post_columns(session, uuid.UUID(post["id"]))

    clock.advance(seconds=age.total_seconds())
    response = edit(alice_client, post["id"])

    assert response.status_code == 409
    assert response.json() == TOO_LATE
    assert post_columns(session, uuid.UUID(post["id"])) == before


def test_the_window_is_exactly_60_minutes() -> None:
    assert posts_service.EDIT_WINDOW == timedelta(minutes=60)


def test_editing_does_not_reset_the_window(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client)  # created at 12:00, say

    clock.advance(minutes=40)  # 12:40
    assert edit(alice_client, post["id"], "First edit").status_code == 200
    clock.advance(minutes=10)  # 12:50
    assert edit(alice_client, post["id"], "Second edit").status_code == 200
    clock.advance(minutes=9, seconds=59)  # 12:59:59
    assert edit(alice_client, post["id"], "Third edit").status_code == 200

    clock.advance(seconds=1)  # 13:00:00
    response = edit(alice_client, post["id"], "Too late")

    # Edited a second ago, but created 60 minutes ago.
    assert response.status_code == 409
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Third edit"


def test_the_window_is_measured_from_created_at_not_updated_at(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account, created_at=minutes_ago(61))
    post.updated_at = minutes_ago(1)
    session.flush()

    assert edit(alice_client, post.id).status_code == 409


def test_post_that_is_too_old_stays_locked(
    alice_client: TestClient, clock
) -> None:
    post = create(alice_client)
    clock.advance(minutes=60)

    for _ in range(3):
        assert edit(alice_client, post["id"]).status_code == 409


def test_the_window_holds_with_the_real_clock(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # No controlled clock here: the rows are moved into the past instead.
    fresh = add_post(session, alice_account, created_at=minutes_ago(59))
    old = add_post(session, alice_account, created_at=minutes_ago(61))

    assert edit(alice_client, fresh.id).status_code == 200
    assert edit(alice_client, old.id).status_code == 409


def test_each_post_has_its_own_window(alice_client: TestClient, clock) -> None:
    older = create(alice_client, "Older")
    clock.advance(minutes=45)
    newer = create(alice_client, "Newer")

    clock.advance(minutes=20)  # older is 65 minutes old, newer 20

    assert edit(alice_client, older["id"]).status_code == 409
    assert edit(alice_client, newer["id"]).status_code == 200


def test_client_cannot_extend_the_window(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client)
    clock.advance(minutes=60)
    just_now = clock.now.isoformat()

    response = alice_client.patch(
        f"/posts/{post['id']}?now={just_now}&editable_until=2099-01-01T00:00:00Z",
        json={
            "content": "Too late",
            "created_at": just_now,
            "updated_at": just_now,
            "editable_until": "2099-01-01T00:00:00Z",
            "now": just_now,
        },
        headers={"Date": "Thu, 01 Jan 2000 00:00:00 GMT"},
    )

    assert response.status_code == 409
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


def test_the_deadline_is_not_stored_anywhere(
    alice_client: TestClient, session: Session, clock
) -> None:
    # Derived from created_at each time, so there is one source of truth.
    post = create(alice_client)

    stored = post_columns(session, uuid.UUID(post["id"]))
    assert not any("edit" in name or "until" in name for name in stored)
    assert not any("edit" in name or "until" in name for name in post)


# --- content -------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["", " ", "   ", "\n\t \n", " 　", "x" * 301, "x" * 1000, None, 123, ["a"]],
)
def test_content_validation_applies_to_edits(
    alice_client: TestClient, session: Session, content: object
) -> None:
    post = create(alice_client, "Original")
    before = post_columns(session, uuid.UUID(post["id"]))

    response = edit(alice_client, post["id"], content)

    assert response.status_code == 422
    assert post_columns(session, uuid.UUID(post["id"])) == before


def test_edit_requires_content(alice_client: TestClient, session: Session) -> None:
    post = create(alice_client, "Original")

    response = alice_client.patch(f"/posts/{post['id']}", json={})

    assert response.status_code == 422
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


def test_edit_accepts_300_characters_and_rejects_301(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")

    at_the_limit = edit(alice_client, post["id"], "😀" * 300)
    over_the_limit = edit(alice_client, post["id"], "😀" * 301)

    assert at_the_limit.status_code == 200
    assert over_the_limit.status_code == 422
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "😀" * 300


def test_edit_accepts_a_single_character(alice_client: TestClient) -> None:
    post = create(alice_client, "Original")

    assert edit(alice_client, post["id"], "a").json()["content"] == "a"


def test_edited_text_is_trimmed_like_new_text(alice_client: TestClient) -> None:
    post = create(alice_client, "Original")

    assert edit(alice_client, post["id"], "  Edited \n").json()["content"] == "Edited"


def test_null_character_is_rejected_on_edit(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")

    assert edit(alice_client, post["id"], "Edi\x00ted").status_code == 422
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


# --- what an edit cannot change ------------------------------------------


def test_update_schema_has_only_the_text() -> None:
    assert set(UpdatePostRequest.model_fields) == {"content"}


def test_parent_post_id_cannot_be_changed(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    first, second = create(alice_client, "First"), create(alice_client, "Second")
    reply = create(alice_client, "Reply", parent_post_id=first["id"])

    moved = edit(alice_client, reply["id"], "Edited", parent_post_id=second["id"])
    detached = edit(alice_client, reply["id"], "Edited again", parent_post_id=None)
    attached = edit(alice_client, second["id"], "Edited", parent_post_id=first["id"])

    assert moved.status_code == detached.status_code == attached.status_code == 200
    assert moved.json()["parent_post_id"] == first["id"]
    assert detached.json()["parent_post_id"] == first["id"]
    assert detached.json()["is_reply"] is True
    assert attached.json()["parent_post_id"] is None
    assert post_columns(session, uuid.UUID(reply["id"]))["parent_post_id"] == uuid.UUID(
        first["id"]
    )
    assert post_columns(session, uuid.UUID(second["id"]))["parent_post_id"] is None


def test_author_id_cannot_be_changed(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    post = create(alice_client)

    response = edit(
        alice_client,
        post["id"],
        "Edited",
        author_id=str(bob_account.id),
        user_id=str(bob_account.id),
        author={"id": str(bob_account.id), "username": "bob"},
    )

    assert response.status_code == 200
    assert response.json()["author"]["username"] == "alice"
    assert post_columns(session, uuid.UUID(post["id"]))["author_id"] == alice_account.id


PROTECTED_COLUMNS = {
    "id": str(uuid.uuid4()),
    "author_id": str(uuid.uuid4()),
    "parent_post_id": str(uuid.uuid4()),
    "created_at": "2099-01-01T00:00:00Z",
    "updated_at": "2000-01-01T00:00:00Z",
    "deleted_at": "2000-01-01T00:00:00Z",
}


@pytest.mark.parametrize("field", PROTECTED_COLUMNS)
def test_protected_field_sent_with_an_edit_is_ignored(
    alice_client: TestClient, session: Session, clock, field: str
) -> None:
    post = create(alice_client, "Original")
    before = post_columns(session, uuid.UUID(post["id"]))

    clock.advance(minutes=1)
    planted = {field: PROTECTED_COLUMNS[field]}
    response = edit(alice_client, post["id"], "Edited", **planted)

    assert response.status_code == 200
    after = post_columns(session, uuid.UUID(post["id"]))
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"content", "updated_at"}
    assert after["updated_at"] == clock.now


@pytest.mark.parametrize("field", PROTECTED_COLUMNS)
def test_protected_field_sent_alone_changes_nothing(
    alice_client: TestClient, session: Session, field: str
) -> None:
    post = create(alice_client, "Original")
    before = post_columns(session, uuid.UUID(post["id"]))

    response = alice_client.patch(
        f"/posts/{post['id']}", json={field: PROTECTED_COLUMNS[field]}
    )

    # Without the text there is nothing to edit.
    assert response.status_code == 422
    assert post_columns(session, uuid.UUID(post["id"])) == before


def test_post_cannot_be_deleted_or_restored_through_an_edit(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")

    edit(alice_client, post["id"], "Edited", deleted_at="2000-01-01T00:00:00Z")
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is None

    alice_client.delete(f"/posts/{post['id']}")
    response = edit(alice_client, post["id"], "Back again", deleted_at=None)

    assert response.status_code == 404
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is not None


# --- who may edit --------------------------------------------------------


def test_another_user_cannot_edit_a_post(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")
    before = post_columns(session, uuid.UUID(post["id"]))

    response = edit(bob_client, post["id"], "Hacked")

    assert response.status_code == 403
    assert response.json() == NOT_THE_AUTHOR
    assert post_columns(session, uuid.UUID(post["id"])) == before


def test_another_user_is_refused_as_not_the_author_also_after_the_window(
    alice_client: TestClient, bob_client: TestClient, clock
) -> None:
    post = create(alice_client)
    clock.advance(hours=2)

    # Who is asking is settled before whether the post can still be edited.
    assert edit(bob_client, post["id"]).status_code == 403
    assert edit(alice_client, post["id"]).status_code == 409


def test_edit_requires_authentication(
    alice_client: TestClient, make_client, session: Session
) -> None:
    post = create(alice_client, "Original")

    response = edit(make_client(), post["id"], "Hacked")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


def test_edit_without_authentication_says_nothing_about_the_post(
    alice_client: TestClient, make_client
) -> None:
    post = create(alice_client)
    alice_client.delete(f"/posts/{post['id']}")
    anonymous = make_client()

    answers = [
        edit(anonymous, post["id"]),  # deleted
        edit(anonymous, uuid.uuid4()),  # never existed
        edit(anonymous, post["id"], ""),  # invalid text
    ]

    assert {response.status_code for response in answers} == {401}
    assert len({response.text for response in answers}) == 1


def test_session_of_an_unverified_account_cannot_edit(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = create(alice_client, "Original")
    alice_account.email_verified_at = None
    session.flush()

    response = edit(alice_client, post["id"])

    assert response.status_code == 403
    assert response.json() == {"detail": "Email address is not verified."}
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


def test_deactivated_author_cannot_edit(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = create(alice_client, "Original")
    alice_account.is_active = False
    session.flush()

    assert edit(alice_client, post["id"]).status_code == 401
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


# --- posts that cannot be edited -----------------------------------------


def test_deleted_post_cannot_be_edited(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")
    alice_client.delete(f"/posts/{post['id']}")
    before = post_columns(session, uuid.UUID(post["id"]))

    response = edit(alice_client, post["id"], "Back again")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert post_columns(session, uuid.UUID(post["id"])) == before


def test_nonexistent_post_cannot_be_edited(alice_client: TestClient) -> None:
    response = edit(alice_client, uuid.uuid4())

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("post_id", ["1", "abc", "me", "00000000-0000-0000-0000"])
def test_malformed_post_id_is_rejected_on_edit(
    alice_client: TestClient, post_id: str
) -> None:
    assert edit(alice_client, post_id).status_code == 422


def test_edit_locks_the_post_while_it_is_checked_and_changed(
    alice_client: TestClient,
) -> None:
    # An edit and a deletion of the same post at the same moment must not
    # both act on what they read. The lock makes the second one wait.
    post = create(alice_client)

    with recorded_selects() as statements:
        edit(alice_client, post["id"])

    assert any("FOR UPDATE OF posts" in statement for statement in statements)
