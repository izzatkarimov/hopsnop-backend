"""Deleting posts: only the author, and the row is kept."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models import Like, Post, Repost, User
from helpers import add_post, post_columns, recorded_selects

NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_FOUND = {"detail": "Post not found."}
NOT_THE_AUTHOR = {"detail": "You are not the author of this post."}


def create(
    client: TestClient, content: str = "Hello Hopsnop!", **extra: object
) -> dict:
    response = client.post("/posts", json={"content": content, **extra})
    assert response.status_code == 201
    return response.json()


def delete(client: TestClient, post_id: object):
    return client.delete(f"/posts/{post_id}")


def post_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Post))


# --- deleting one's own post ---------------------------------------------


def test_author_can_delete_their_post(alice_client: TestClient) -> None:
    post = create(alice_client)

    response = delete(alice_client, post["id"])

    assert response.status_code == 204
    assert response.content == b""


def test_delete_sets_deleted_at(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client)
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is None

    clock.advance(minutes=5)
    delete(alice_client, post["id"])

    deleted_at = post_columns(session, uuid.UUID(post["id"]))["deleted_at"]
    assert deleted_at == clock.now
    assert deleted_at.tzinfo is not None


def test_deleted_post_is_still_in_the_database(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Keep me on record")

    delete(alice_client, post["id"])

    assert post_count(session) == 1
    stored = post_columns(session, uuid.UUID(post["id"]))
    assert stored["content"] == "Keep me on record"


def test_delete_changes_nothing_but_deleted_at(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client, "Original")
    clock.advance(minutes=10)
    alice_client.patch(f"/posts/{post['id']}", json={"content": "Edited"})
    before = post_columns(session, uuid.UUID(post["id"]))

    clock.advance(minutes=10)
    delete(alice_client, post["id"])

    after = post_columns(session, uuid.UUID(post["id"]))
    changed = {name for name in before if before[name] != after[name]}
    # In particular updated_at still says when the text was last edited.
    assert changed == {"deleted_at"}


def test_delete_only_affects_the_post_it_names(
    alice_client: TestClient, session: Session
) -> None:
    first, second = create(alice_client, "First"), create(alice_client, "Second")

    delete(alice_client, first["id"])

    assert post_columns(session, uuid.UUID(second["id"]))["deleted_at"] is None
    assert alice_client.get(f"/posts/{second['id']}").status_code == 200


def test_post_of_any_age_can_be_deleted(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # The 60 minutes only limit editing.
    old = add_post(
        session,
        alice_account,
        created_at=datetime.now(timezone.utc) - timedelta(days=400),
    )

    assert delete(alice_client, old.id).status_code == 204
    assert post_columns(session, old.id)["deleted_at"] is not None


def test_delete_ignores_a_request_body(
    alice_client: TestClient, session: Session, bob_account: User, clock
) -> None:
    post = create(alice_client)

    response = alice_client.request(
        "DELETE",
        f"/posts/{post['id']}",
        json={
            "deleted_at": "2000-01-01T00:00:00Z",
            "author_id": str(bob_account.id),
            "hard": True,
        },
    )

    assert response.status_code == 204
    stored = post_columns(session, uuid.UUID(post["id"]))
    assert stored["deleted_at"] == clock.now
    assert post_count(session) == 1


# --- a deleted post is gone for its readers ------------------------------


def test_deleted_post_is_excluded_from_reads(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = create(alice_client, "Regretted")
    delete(alice_client, post["id"])

    for viewer in (make_client(), bob_client, alice_client):
        response = viewer.get(f"/posts/{post['id']}")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert "Regretted" not in response.text


def test_deleted_post_is_excluded_from_the_users_posts(
    alice_client: TestClient, make_client
) -> None:
    kept, removed = create(alice_client, "Kept"), create(alice_client, "Removed")

    delete(alice_client, removed["id"])

    for viewer in (make_client(), alice_client):
        items = viewer.get("/users/alice/posts").json()["items"]
        assert [item["id"] for item in items] == [kept["id"]]


def test_deleted_post_cannot_be_edited(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")
    delete(alice_client, post["id"])

    response = alice_client.patch(f"/posts/{post['id']}", json={"content": "Edited"})

    assert response.status_code == 404
    assert post_columns(session, uuid.UUID(post["id"]))["content"] == "Original"


def test_repeated_deletion_is_not_found_and_changes_nothing(
    alice_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client)
    assert delete(alice_client, post["id"]).status_code == 204
    first_deleted_at = post_columns(session, uuid.UUID(post["id"]))["deleted_at"]

    clock.advance(minutes=5)
    again = [delete(alice_client, post["id"]) for _ in range(2)]

    assert [response.status_code for response in again] == [404, 404]
    assert again[0].json() == NOT_FOUND
    # The moment of the actual deletion is kept.
    stored = post_columns(session, uuid.UUID(post["id"]))
    assert stored["deleted_at"] == first_deleted_at
    assert post_count(session) == 1


def test_deleted_post_cannot_be_brought_back(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    delete(alice_client, post["id"])

    for method in ("POST", "PUT", "PATCH"):
        alice_client.request(
            method,
            f"/posts/{post['id']}",
            json={"content": "Back", "deleted_at": None},
        )

    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is not None
    assert alice_client.get(f"/posts/{post['id']}").status_code == 404


# --- what is kept --------------------------------------------------------


def test_delete_keeps_likes_reposts_and_replies(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    post = create(alice_client)
    post_id = uuid.UUID(post["id"])
    reply = add_post(session, bob_account, "Reply", parent=session.get(Post, post_id))
    session.add_all(
        [
            Like(user_id=bob_account.id, post_id=post_id),
            Repost(user_id=bob_account.id, post_id=post_id),
        ]
    )
    session.flush()

    assert delete(alice_client, post["id"]).status_code == 204

    assert session.scalar(select(func.count()).select_from(Like)) == 1
    assert session.scalar(select(func.count()).select_from(Repost)) == 1
    assert post_count(session) == 2
    assert post_columns(session, reply.id)["parent_post_id"] == post_id
    assert post_columns(session, reply.id)["deleted_at"] is None


def test_reply_to_a_deleted_post_stays_readable(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = create(alice_client, "Parent")
    reply = create(bob_client, "Reply", parent_post_id=post["id"])

    delete(alice_client, post["id"])

    response = make_client().get(f"/posts/{reply['id']}")
    assert response.status_code == 200
    # Still a reply, to a post that is no longer shown.
    assert response.json()["parent_post_id"] == post["id"]
    assert response.json()["is_reply"] is True


def test_deleting_a_reply_leaves_its_parent(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Parent")
    reply = create(bob_client, "Reply", parent_post_id=post["id"])

    assert delete(bob_client, reply["id"]).status_code == 204

    assert alice_client.get(f"/posts/{post['id']}").status_code == 200
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is None
    assert post_columns(session, uuid.UUID(reply["id"]))["deleted_at"] is not None


def test_deleting_a_thread_post_by_post_keeps_every_row(
    alice_client: TestClient, session: Session
) -> None:
    first = create(alice_client, "A")
    second = create(alice_client, "B", parent_post_id=first["id"])
    third = create(alice_client, "C", parent_post_id=second["id"])

    for post in (first, second, third):
        assert delete(alice_client, post["id"]).status_code == 204

    assert post_count(session) == 3
    assert post_columns(session, uuid.UUID(third["id"]))["parent_post_id"] == uuid.UUID(
        second["id"]
    )


# --- who may delete ------------------------------------------------------


def test_another_user_cannot_delete_a_post(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    before = post_columns(session, uuid.UUID(post["id"]))

    response = delete(bob_client, post["id"])

    assert response.status_code == 403
    assert response.json() == NOT_THE_AUTHOR
    assert post_columns(session, uuid.UUID(post["id"])) == before
    assert alice_client.get(f"/posts/{post['id']}").status_code == 200


def test_author_of_the_parent_cannot_delete_a_reply(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Parent")
    reply = create(bob_client, "Reply", parent_post_id=post["id"])

    # Owning the post that was replied to gives no say over the reply.
    assert delete(alice_client, reply["id"]).status_code == 403
    assert post_columns(session, uuid.UUID(reply["id"]))["deleted_at"] is None


def test_delete_requires_authentication(
    alice_client: TestClient, make_client, session: Session
) -> None:
    post = create(alice_client)

    response = delete(make_client(), post["id"])

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is None


def test_session_of_an_unverified_account_cannot_delete(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = create(alice_client)
    alice_account.email_verified_at = None
    session.flush()

    assert delete(alice_client, post["id"]).status_code == 403
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is None


def test_deactivated_author_cannot_delete(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = create(alice_client)
    alice_account.is_active = False
    session.flush()

    assert delete(alice_client, post["id"]).status_code == 401
    assert post_columns(session, uuid.UUID(post["id"]))["deleted_at"] is None


def test_nonexistent_post_cannot_be_deleted(alice_client: TestClient) -> None:
    response = delete(alice_client, uuid.uuid4())

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("post_id", ["1", "abc", "me", "00000000-0000-0000-0000"])
def test_malformed_post_id_is_rejected_on_delete(
    alice_client: TestClient, session: Session, post_id: str
) -> None:
    create(alice_client)

    assert delete(alice_client, post_id).status_code == 422
    assert session.scalar(select(func.count()).where(Post.deleted_at.is_not(None))) == 0


def test_posts_cannot_be_deleted_in_bulk(
    alice_client: TestClient, session: Session
) -> None:
    create(alice_client)

    assert alice_client.delete("/posts").status_code == 405
    assert alice_client.delete("/users/alice/posts").status_code == 405
    assert session.scalar(select(func.count()).where(Post.deleted_at.is_not(None))) == 0


def test_delete_locks_the_post_while_it_is_checked_and_marked(
    alice_client: TestClient,
) -> None:
    post = create(alice_client)

    with recorded_selects() as statements:
        delete(alice_client, post["id"])

    assert any("FOR UPDATE OF posts" in statement for statement in statements)


def test_delete_never_issues_a_delete_statement(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    post = create(alice_client)
    reply = create(bob_client, "Reply", parent_post_id=post["id"])
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        delete(bob_client, reply["id"])
        delete(alice_client, post["id"])
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert any(statement.startswith("UPDATE POSTS") for statement in statements)
    assert not any(statement.startswith("DELETE") for statement in statements)
