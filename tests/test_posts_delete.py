"""Deleting posts: only the author, and what is deleted is erased.

A post that nobody has replied to leaves no row. One that has replies leaves
a tombstone: a row without author and without text that keeps the thread in
one piece, and that goes when its last reply does.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models import Like, Post, Repost, User
from helpers import (
    add_post,
    follow,
    post_columns,
    recorded_selects,
    stored_post,
    where_text_is_stored,
)

NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_FOUND = {"detail": "Post not found."}
PARENT_NOT_FOUND = {"detail": "Parent post not found."}
NOT_THE_AUTHOR = {"detail": "You are not the author of this post."}
# What a tombstone keeps of the post it was.
KEPT_IN_A_TOMBSTONE = {"id", "parent_post_id", "created_at", "updated_at"}


def create(
    client: TestClient, content: str = "Hello Hopsnop!", **extra: object
) -> dict:
    response = client.post("/posts", json={"content": content, **extra})
    assert response.status_code == 201
    return response.json()


def reply(client: TestClient, to: dict, content: str = "Reply") -> dict:
    return create(client, content, parent_post_id=to["id"])


def delete(client: TestClient, post_id: object):
    return client.delete(f"/posts/{post_id}")


def post_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Post))


def stored(session: Session, post: dict) -> dict[str, object] | None:
    return stored_post(session, uuid.UUID(post["id"]))


def interactions(session: Session) -> tuple[int, int]:
    """How many likes and how many reposts there are, of any post."""
    return (
        session.scalar(select(func.count()).select_from(Like)),
        session.scalar(select(func.count()).select_from(Repost)),
    )


# --- deleting one's own post ---------------------------------------------


def test_author_can_delete_their_post(alice_client: TestClient) -> None:
    post = create(alice_client)

    response = delete(alice_client, post["id"])

    assert response.status_code == 204
    assert response.content == b""


def test_deleted_post_that_nobody_replied_to_leaves_no_row(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Not for the record")
    assert post_count(session) == 1

    delete(alice_client, post["id"])

    assert stored(session, post) is None
    assert post_count(session) == 0


def test_nothing_of_a_deleted_post_is_left_anywhere_in_the_database(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "A sentence found nowhere else: zyzzyva-41")
    alice_client.patch(
        f"/posts/{post['id']}", json={"content": "Edited sentence: zyzzyva-42"}
    )
    assert where_text_is_stored(session, "zyzzyva-42") == ["posts.content"]

    delete(alice_client, post["id"])

    # Not in the row, which is gone, and not kept aside in any other table.
    assert where_text_is_stored(session, "zyzzyva") == []


def test_delete_only_affects_the_post_it_names(
    alice_client: TestClient, session: Session
) -> None:
    first, second = create(alice_client, "First"), create(alice_client, "Second")
    before = stored(session, second)

    delete(alice_client, first["id"])

    assert stored(session, first) is None
    assert stored(session, second) == before
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
    old_id = old.id

    assert delete(alice_client, old_id).status_code == 204
    assert stored_post(session, old_id) is None


def test_delete_ignores_a_request_body(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    answered = create(alice_client, "Answered")
    reply(bob_client, answered)
    bobs = create(bob_client, "Bob's own")

    for doomed in (post, answered):
        response = alice_client.request(
            "DELETE",
            f"/posts/{doomed['id']}",
            json={
                "deleted_at": "2000-01-01T00:00:00Z",
                "keep": True,
                "soft": True,
                "content": "Kept after all",
                "post_id": bobs["id"],
            },
        )
        assert response.status_code == 204

    # Erased as it would have been without one: nothing a client sends
    # keeps the text, moves the time, or points the deletion at another post.
    assert stored(session, post) is None
    assert stored(session, answered)["content"] is None
    assert stored(session, answered)["deleted_at"].year == datetime.now().year
    assert where_text_is_stored(session, "Kept after all") == []
    assert stored(session, bobs)["content"] == "Bob's own"


def test_deleting_a_post_removes_its_likes_and_reposts(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post, other = create(alice_client), create(alice_client, "Other")
    for client in (alice_client, bob_client):
        client.post(f"/posts/{post['id']}/like")
        client.post(f"/posts/{post['id']}/repost")
    bob_client.post(f"/posts/{other['id']}/like")
    assert interactions(session) == (3, 2)

    delete(alice_client, post["id"])

    # Those of the deleted post, by whoever, and no others.
    assert interactions(session) == (1, 0)
    assert bob_client.get(f"/posts/{other['id']}").json()["like_count"] == 1


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


def test_deleted_post_is_excluded_from_every_list(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    kept, removed = create(alice_client, "Kept"), create(alice_client, "Removed")

    delete(alice_client, removed["id"])

    for viewer in (make_client(), alice_client, bob_client):
        for path in ("/users/alice/posts", "/feed"):
            items = viewer.get(path).json()["items"]
            assert [item["id"] for item in items] == [kept["id"]], path
    items = bob_client.get("/feed/following").json()["items"]
    assert [item["id"] for item in items] == [kept["id"]]


def test_deleted_post_cannot_be_edited(
    alice_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Original")
    delete(alice_client, post["id"])

    response = alice_client.patch(f"/posts/{post['id']}", json={"content": "Edited"})

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert post_count(session) == 0


def test_repeated_deletion_is_not_found_and_changes_nothing(
    alice_client: TestClient, session: Session
) -> None:
    post, other = create(alice_client), create(alice_client, "Other")
    assert delete(alice_client, post["id"]).status_code == 204
    before = stored(session, other)

    again = [delete(alice_client, post["id"]) for _ in range(2)]

    assert [response.status_code for response in again] == [404, 404]
    assert again[0].json() == NOT_FOUND
    assert post_count(session) == 1
    assert stored(session, other) == before


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

    assert post_count(session) == 0
    assert alice_client.get(f"/posts/{post['id']}").status_code == 404


# --- a post that has replies leaves a tombstone --------------------------


def test_deleting_a_post_with_replies_leaves_a_tombstone_and_nothing_else(
    alice_client: TestClient, bob_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client, "The post that was answered")
    answer = reply(bob_client, post)
    before = stored(session, post)

    clock.advance(minutes=5)
    assert delete(alice_client, post["id"]).status_code == 204

    after = stored(session, post)
    # The two things that made it a post of someone's are gone...
    assert after["author_id"] is None
    assert after["content"] is None
    # ...it is marked as erased, at the moment it was...
    assert after["deleted_at"] == clock.now
    assert after["deleted_at"].tzinfo is not None
    # ...and what its reply needs to stay in place is what it was.
    assert {name: after[name] for name in KEPT_IN_A_TOMBSTONE} == {
        name: before[name] for name in KEPT_IN_A_TOMBSTONE
    }
    # A row has no column beyond these.
    assert set(after) == KEPT_IN_A_TOMBSTONE | {"author_id", "content", "deleted_at"}
    assert stored(session, answer)["parent_post_id"] == uuid.UUID(post["id"])
    assert post_count(session) == 2


def test_tombstone_holds_nothing_that_says_whose_it_was_or_what_it_said(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
) -> None:
    post = create(alice_client, "Words that must be gone: quokka-77")
    reply(bob_client, post, "A reply that stays: wombat-12")

    delete(alice_client, post["id"])

    tombstone = stored(session, post)
    assert where_text_is_stored(session, "quokka-77") == []
    assert where_text_is_stored(session, "wombat-12") == ["posts.content"]
    # Nothing in the row is the author's id, name or address.
    for value in tombstone.values():
        assert value != alice_account.id
        assert "alice" not in str(value)


def test_tombstone_keeps_the_time_of_the_last_edit_as_it_was(
    alice_client: TestClient, bob_client: TestClient, session: Session, clock
) -> None:
    post = create(alice_client, "Original")
    clock.advance(minutes=10)
    alice_client.patch(f"/posts/{post['id']}", json={"content": "Edited"})
    reply(bob_client, post)
    before = stored(session, post)

    clock.advance(minutes=10)
    delete(alice_client, post["id"])

    after = stored(session, post)
    # Erasing is not an edit: only the three columns it is about changed.
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"author_id", "content", "deleted_at"}
    assert after["updated_at"] > after["created_at"]


def test_tombstone_has_no_likes_and_no_reposts(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    answer = reply(bob_client, post)
    for client in (alice_client, bob_client):
        client.post(f"/posts/{post['id']}/like")
        client.post(f"/posts/{post['id']}/repost")
    alice_client.post(f"/posts/{answer['id']}/like")
    assert interactions(session) == (3, 2)

    delete(alice_client, post["id"])

    # The row stays, for its reply. What was liked and reposted does not,
    # so nothing is left that could be liked or reposted. The reply's own
    # like is not the post's and stays.
    assert stored(session, post) is not None
    assert interactions(session) == (1, 0)
    assert bob_client.get(f"/posts/{answer['id']}").json()["like_count"] == 1


def test_replies_of_a_deleted_post_stay_what_they_were(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
) -> None:
    post = create(alice_client, "Parent")
    answer = reply(bob_client, post, "Bob's reply")
    before = stored(session, answer)

    delete(alice_client, post["id"])

    assert stored(session, answer) == before
    for viewer in (make_client(), alice_client, bob_client):
        response = viewer.get(f"/posts/{answer['id']}")
        assert response.status_code == 200
        # Still a reply, to a post that is no longer shown.
        assert response.json()["parent_post_id"] == post["id"]
        assert response.json()["is_reply"] is True
        assert response.json()["content"] == "Bob's reply"
        feed = viewer.get("/feed").json()["items"]
        assert [item["id"] for item in feed] == [answer["id"]]
        listed = viewer.get("/users/bob/posts").json()["items"]
        assert [item["id"] for item in listed] == [answer["id"]]


def test_reply_of_a_deleted_post_can_still_be_answered_liked_and_edited(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    post = create(alice_client, "Parent")
    answer = reply(bob_client, post)
    delete(alice_client, post["id"])

    nested = alice_client.post(
        "/posts", json={"content": "Nested", "parent_post_id": answer["id"]}
    )
    liked = alice_client.post(f"/posts/{answer['id']}/like")
    edited = bob_client.patch(f"/posts/{answer['id']}", json={"content": "Edited"})
    listed = alice_client.get(f"/posts/{answer['id']}/replies")

    assert (nested.status_code, liked.status_code, edited.status_code) == (
        201,
        200,
        200,
    )
    assert [item["id"] for item in listed.json()["items"]] == [nested.json()["id"]]


@pytest.mark.parametrize("who", ["anonymous", "another user", "the former author"])
def test_tombstone_is_not_found_whatever_is_asked_of_it(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    who: str,
) -> None:
    post = create(alice_client, "Erased words")
    reply(bob_client, post)
    delete(alice_client, post["id"])
    client = {
        "anonymous": make_client(),
        "another user": bob_client,
        "the former author": alice_client,
    }[who]
    before = stored(session, post)
    path = f"/posts/{post['id']}"

    answers = {
        "read": client.get(path),
        "replies": client.get(f"{path}/replies"),
        "edit": client.patch(path, json={"content": "Back"}),
        "delete": client.delete(path),
        "like": client.post(f"{path}/like"),
        "unlike": client.delete(f"{path}/like"),
        "repost": client.post(f"{path}/repost"),
        "unrepost": client.delete(f"{path}/repost"),
        "reply": client.post(
            "/posts", json={"content": "To a tombstone", "parent_post_id": post["id"]}
        ),
    }

    # What a post that never existed is answered with: 404 for whoever is
    # signed in, and for nobody a way to learn that a row is there.
    expected = 404 if who != "anonymous" else 401
    for name, response in answers.items():
        status = 404 if name in ("read", "replies") else expected
        assert response.status_code == status, name
        assert "Erased words" not in response.text
    if who != "anonymous":
        assert answers["reply"].json() == PARENT_NOT_FOUND
        assert answers["delete"].json() == NOT_FOUND
    assert stored(session, post) == before
    assert post_count(session) == 2
    assert interactions(session) == (0, 0)


def test_tombstone_is_in_no_list_and_no_feed(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, alice_account, bob_account)
    root = create(alice_client, "Root")
    middle = reply(alice_client, root, "Middle, to be erased")
    leaf = reply(bob_client, middle, "Leaf")

    delete(alice_client, middle["id"])

    def item_ids(client: TestClient, path: str) -> list[str]:
        response = client.get(path)
        assert response.status_code == 200
        assert "to be erased" not in response.text
        return [item["id"] for item in response.json()["items"]]

    for viewer in (make_client(), alice_client, bob_client):
        assert item_ids(viewer, "/feed") == [leaf["id"], root["id"]]
        assert item_ids(viewer, "/users/alice/posts") == [root["id"]]
        assert item_ids(viewer, "/users/bob/posts") == [leaf["id"]]
        # No longer among the replies of the post it answered...
        assert item_ids(viewer, f"/posts/{root['id']}/replies") == []
        # ...and what answered it cannot be listed through it.
        assert viewer.get(f"/posts/{middle['id']}/replies").status_code == 404
    assert item_ids(bob_client, "/feed/following") == [root["id"]]
    assert item_ids(alice_client, "/feed/following") == [leaf["id"]]


def test_post_responses_are_made_as_they_were(alice_client: TestClient) -> None:
    from app.main import app

    schema = app.openapi()["components"]["schemas"]["PostResponse"]

    # A tombstone is never sent, so a post in a response still always has an
    # author and a text, and there is no field that says "deleted".
    assert schema["properties"]["author"] == {
        "$ref": "#/components/schemas/PostAuthorResponse"
    }
    assert schema["properties"]["content"]["type"] == "string"
    assert {"author", "content"} <= set(schema["required"])
    assert not {"deleted", "deleted_at", "is_deleted"} & set(schema["properties"])


# --- a tombstone goes when its last reply does ---------------------------


def test_deleting_the_last_reply_removes_the_tombstone_above_it(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Parent")
    answer = reply(bob_client, post)
    delete(alice_client, post["id"])
    assert stored(session, post) is not None

    assert delete(bob_client, answer["id"]).status_code == 204

    # It was only there for the reply.
    assert stored(session, answer) is None
    assert stored(session, post) is None
    assert post_count(session) == 0


def test_tombstone_stays_while_any_reply_is_left(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Parent")
    answers = [reply(bob_client, post, f"Reply {number}") for number in range(3)]
    delete(alice_client, post["id"])

    for answer in answers[:2]:
        delete(bob_client, answer["id"])
        assert stored(session, post) is not None
    assert post_count(session) == 2

    delete(bob_client, answers[2]["id"])

    assert post_count(session) == 0


def test_removal_goes_up_the_thread_through_every_tombstone_it_empties(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    first = create(alice_client, "A")
    second = reply(bob_client, first, "B")
    third = reply(alice_client, second, "C")
    fourth = reply(bob_client, third, "D")
    for client, post in ((alice_client, first), (bob_client, second)):
        delete(client, post["id"])
    delete(alice_client, third["id"])
    # Three tombstones in a row, each kept for the one below it.
    assert post_count(session) == 4

    delete(bob_client, fourth["id"])

    assert post_count(session) == 0


def test_removal_stops_at_a_post_that_was_not_deleted(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    root = create(alice_client, "Root, never deleted")
    middle = reply(bob_client, root, "Middle")
    leaf = reply(alice_client, middle, "Leaf")
    delete(bob_client, middle["id"])
    before = stored(session, root)

    delete(alice_client, leaf["id"])

    # The tombstone goes with its last reply. The post above it is a post,
    # and losing its last reply is nothing to a post.
    assert stored(session, middle) is None
    assert stored(session, root) == before
    assert alice_client.get(f"/posts/{root['id']}").status_code == 200
    assert post_count(session) == 1


def test_removal_stops_at_a_tombstone_that_has_another_reply(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    root = create(alice_client, "Root")
    branch = reply(bob_client, root, "Branch")
    other = reply(bob_client, root, "Another reply to the root")
    leaf = reply(alice_client, branch, "Leaf")
    delete(alice_client, root["id"])
    delete(bob_client, branch["id"])

    delete(alice_client, leaf["id"])

    assert stored(session, branch) is None
    # Still needed: something else answers it.
    assert stored(session, root)["content"] is None
    assert stored(session, other)["parent_post_id"] == uuid.UUID(root["id"])
    assert post_count(session) == 2


def test_deleting_a_thread_from_the_top_ends_with_nothing_left(
    alice_client: TestClient, session: Session
) -> None:
    first = create(alice_client, "A")
    second = reply(alice_client, first, "B")
    third = reply(alice_client, second, "C")
    counts = []

    for post in (first, second, third):
        assert delete(alice_client, post["id"]).status_code == 204
        counts.append(post_count(session))

    # Two tombstones on the way, each kept for what was still below it, and
    # all of it gone with the last post.
    assert counts == [3, 3, 0]


def test_deleting_a_thread_from_the_bottom_never_leaves_a_tombstone(
    alice_client: TestClient, session: Session
) -> None:
    first = create(alice_client, "A")
    second = reply(alice_client, first, "B")
    third = reply(alice_client, second, "C")
    counts = []

    for post in (third, second, first):
        assert delete(alice_client, post["id"]).status_code == 204
        counts.append(post_count(session))
        assert session.scalar(
            select(func.count()).where(Post.deleted_at.is_not(None))
        ) == 0

    assert counts == [2, 1, 0]


def test_deleting_a_reply_leaves_its_parent(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Parent")
    answer = reply(bob_client, post)
    before = stored(session, post)

    assert delete(bob_client, answer["id"]).status_code == 204

    assert alice_client.get(f"/posts/{post['id']}").status_code == 200
    assert stored(session, post) == before
    assert stored(session, answer) is None


# --- who may delete ------------------------------------------------------


def test_another_user_cannot_delete_a_post(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    bob_client.post(f"/posts/{post['id']}/like")
    before = post_columns(session, uuid.UUID(post["id"]))

    response = delete(bob_client, post["id"])

    assert response.status_code == 403
    assert response.json() == NOT_THE_AUTHOR
    assert post_columns(session, uuid.UUID(post["id"])) == before
    assert interactions(session) == (1, 0)
    assert alice_client.get(f"/posts/{post['id']}").status_code == 200


def test_author_of_the_parent_cannot_delete_a_reply(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client, "Parent")
    answer = reply(bob_client, post)

    # Owning the post that was replied to gives no say over the reply.
    assert delete(alice_client, answer["id"]).status_code == 403
    assert stored(session, answer)["content"] == "Reply"


def test_post_that_is_not_shown_is_not_found_rather_than_forbidden(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account, "Hidden with its author")
    post_id = post.id
    alice_account.is_active = False
    session.flush()

    response = delete(bob_client, post_id)

    # Not "you are not the author": that would say there is such a post.
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert stored_post(session, post_id)["content"] == "Hidden with its author"


def test_delete_requires_authentication(
    alice_client: TestClient, make_client, session: Session
) -> None:
    post = create(alice_client)

    response = delete(make_client(), post["id"])

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert stored(session, post)["deleted_at"] is None


def test_session_of_an_unverified_account_cannot_delete(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = create(alice_client)
    alice_account.email_verified_at = None
    session.flush()

    assert delete(alice_client, post["id"]).status_code == 403
    assert stored(session, post)["deleted_at"] is None


def test_deactivated_author_cannot_delete(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = create(alice_client)
    alice_account.is_active = False
    session.flush()

    assert delete(alice_client, post["id"]).status_code == 401
    assert stored(session, post)["deleted_at"] is None


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
    assert post_count(session) == 1


def test_posts_cannot_be_deleted_in_bulk(
    alice_client: TestClient, session: Session
) -> None:
    create(alice_client)

    assert alice_client.delete("/posts").status_code == 405
    assert alice_client.delete("/users/alice/posts").status_code == 405
    assert post_count(session) == 1


# --- how it is done ------------------------------------------------------


def statements_of(action) -> list[str]:
    """Every statement sent to the database while ``action`` runs."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statement = " ".join(statement.split())
        if not statement.upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


def test_delete_locks_the_post_while_it_is_checked_and_erased(
    alice_client: TestClient,
) -> None:
    post = create(alice_client)

    with recorded_selects() as statements:
        delete(alice_client, post["id"])

    assert any("FOR UPDATE OF posts" in statement for statement in statements)


def test_delete_takes_the_erasure_lock_before_it_locks_the_post(
    alice_client: TestClient,
) -> None:
    post = create(alice_client)

    statements = statements_of(lambda: delete(alice_client, post["id"]))

    erasure_lock = next(
        index for index, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql
    )
    post_lock = next(
        index for index, sql in enumerate(statements) if "FOR UPDATE OF posts" in sql
    )
    # The order every erasure takes the two in. An account being deleted
    # holds the first while it waits for the second; the other way round the
    # two could wait for each other.
    assert erasure_lock < post_lock


def test_delete_removes_the_row_and_does_not_only_mark_it(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    post = create(alice_client)
    answer = reply(bob_client, post)

    leaf = statements_of(lambda: delete(bob_client, answer["id"]))

    assert any(sql.upper().startswith("DELETE FROM POSTS") for sql in leaf)
    assert any(sql.upper().startswith("DELETE FROM LIKES") for sql in leaf)
    assert any(sql.upper().startswith("DELETE FROM REPOSTS") for sql in leaf)


def test_delete_takes_the_same_statements_however_much_there_is_to_remove(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    bob_account: User,
) -> None:
    def deletion_of_a_post_with(replies: int, likes: int) -> list[str]:
        post = create(alice_client, "To be deleted")
        post_id = uuid.UUID(post["id"])
        for number in range(replies):
            reply(bob_client, post, f"Reply {number}")
        fans = [bob_account] + [
            _fan(session, f"fan_{replies}_{number}") for number in range(likes - 1)
        ]
        for fan in fans[:likes]:
            session.add_all(
                [
                    Like(user_id=fan.id, post_id=post_id),
                    Repost(user_id=fan.id, post_id=post_id),
                ]
            )
        session.flush()
        return statements_of(lambda: delete(alice_client, post["id"]))

    few = deletion_of_a_post_with(replies=1, likes=1)
    many = deletion_of_a_post_with(replies=25, likes=25)

    # No statement per reply, per like or per repost.
    assert len(many) == len(few)
    assert [sql.split(" ")[0] for sql in many] == [sql.split(" ")[0] for sql in few]


def _fan(session: Session, username: str) -> User:
    from helpers import add_user

    return add_user(session, username)


def test_removal_up_the_thread_takes_statements_by_its_depth_only(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    def deletion_of_the_last_reply_under(tombstones: int) -> list[str]:
        chain = [create(alice_client, "Top")]
        for number in range(tombstones):
            chain.append(reply(alice_client, chain[-1], f"Level {number}"))
        for post in chain[:tombstones]:
            delete(alice_client, post["id"])
        return statements_of(lambda: delete(alice_client, chain[-1]["id"]))

    one, three = (deletion_of_the_last_reply_under(depth) for depth in (1, 3))

    # Each tombstone above costs a lock and a removal, and nothing else does.
    assert len(three) - len(one) == 2 * 2
    assert post_count(session) == 0
