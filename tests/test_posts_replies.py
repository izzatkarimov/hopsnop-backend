"""Replies: posts that answer another post, and follow every rule of their own."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Post, User
from helpers import add_post, add_user, log_in, post_columns

NOT_FOUND = {"detail": "Post not found."}
PARENT_NOT_FOUND = {"detail": "Parent post not found."}


def create(
    client: TestClient, content: str = "Hello Hopsnop!", **extra: object
) -> dict:
    response = client.post("/posts", json={"content": content, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def reply(client: TestClient, parent_id: object, content: object = "Nice post!"):
    return client.post(
        "/posts", json={"content": content, "parent_post_id": str(parent_id)}
    )


def post_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Post))


def make_private(session: Session, user: User) -> None:
    user.is_private = True
    session.flush()


# --- replying ------------------------------------------------------------


def test_user_can_reply_to_a_post_they_can_see(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    bob_account: User,
) -> None:
    post = create(alice_client)

    response = reply(bob_client, post["id"], "Nice post!")

    assert response.status_code == 201
    body = response.json()
    assert body["content"] == "Nice post!"
    assert body["parent_post_id"] == post["id"]
    assert body["is_reply"] is True
    assert body["author"]["username"] == "bob"
    stored = post_columns(session, uuid.UUID(body["id"]))
    assert stored["parent_post_id"] == uuid.UUID(post["id"])
    assert stored["author_id"] == bob_account.id


def test_reply_is_a_post_in_its_own_right(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = create(alice_client)
    created = reply(bob_client, post["id"]).json()

    # Same shape, same endpoint, its own id.
    assert set(created) == set(post)
    assert created["id"] != post["id"]
    assert make_client().get(f"/posts/{created['id']}").json() == created


def test_user_can_reply_to_their_own_post(alice_client: TestClient) -> None:
    post = create(alice_client)

    response = reply(alice_client, post["id"], "And another thing")

    assert response.status_code == 201
    assert response.json()["parent_post_id"] == post["id"]


def test_post_can_get_several_replies(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)

    repliers = (bob_client, bob_client, alice_client)
    replies = [reply(client, post["id"]) for client in repliers]

    assert [response.status_code for response in replies] == [201, 201, 201]
    assert {response.json()["parent_post_id"] for response in replies} == {post["id"]}
    assert post_count(session) == 4


def test_replying_does_not_change_the_parent(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    before = post_columns(session, uuid.UUID(post["id"]))

    reply(bob_client, post["id"])

    assert post_columns(session, uuid.UUID(post["id"])) == before


def test_reply_appears_among_its_authors_posts(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = create(alice_client)
    created = reply(bob_client, post["id"]).json()

    anonymous = make_client()

    assert anonymous.get("/users/bob/posts").json()["items"] == [created]
    assert anonymous.get("/users/alice/posts").json()["items"] == [post]


# --- nested replies ------------------------------------------------------


def test_reply_can_itself_be_replied_to(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    post = create(alice_client, "A")
    first = reply(bob_client, post["id"], "B").json()

    second = reply(alice_client, first["id"], "C")

    assert second.status_code == 201
    assert second.json()["parent_post_id"] == first["id"]
    assert second.json()["is_reply"] is True


def test_replies_can_be_nested_deeply(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    thread = [create(alice_client, "Start")]
    for depth in range(1, 8):
        client = bob_client if depth % 2 else alice_client
        thread.append(reply(client, thread[-1]["id"], f"Depth {depth}").json())

    # Each one points at the one before it and at nothing else.
    for parent, child in zip(thread, thread[1:]):
        assert child["parent_post_id"] == parent["id"]
    assert thread[0]["parent_post_id"] is None
    assert post_count(session) == 8


def test_reply_points_at_its_direct_parent_not_the_start_of_the_thread(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    post = create(alice_client, "A")
    first = reply(bob_client, post["id"], "B").json()
    second = reply(alice_client, first["id"], "C").json()

    assert second["parent_post_id"] == first["id"]
    assert second["parent_post_id"] != post["id"]


# --- content -------------------------------------------------------------


def test_reply_is_limited_to_300_characters(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)

    at_the_limit = reply(bob_client, post["id"], "😀" * 300)
    over_the_limit = reply(bob_client, post["id"], "😀" * 301)

    assert at_the_limit.status_code == 201
    assert over_the_limit.status_code == 422
    assert post_count(session) == 2


@pytest.mark.parametrize("content", ["", "   ", "\n\t", None])
def test_reply_needs_text_like_any_post(
    alice_client: TestClient, bob_client: TestClient, session: Session, content: object
) -> None:
    post = create(alice_client)

    assert reply(bob_client, post["id"], content).status_code == 422
    assert post_count(session) == 1


# --- parents that cannot be replied to -----------------------------------


def test_cannot_reply_to_a_nonexistent_post(
    alice_client: TestClient, session: Session
) -> None:
    response = reply(alice_client, uuid.uuid4())

    assert response.status_code == 404
    assert response.json() == PARENT_NOT_FOUND
    assert post_count(session) == 0


def test_cannot_reply_to_a_deleted_post(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    alice_client.delete(f"/posts/{post['id']}")

    # Its author cannot either.
    for client in (bob_client, alice_client):
        response = reply(client, post["id"])
        assert response.status_code == 404
        assert response.json() == PARENT_NOT_FOUND

    assert post_count(session) == 1


def test_cannot_reply_to_a_private_accounts_post(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
) -> None:
    make_private(session, alice_account)
    post = create(alice_client, "For my eyes only")

    response = reply(bob_client, post["id"])

    assert response.status_code == 404
    assert response.json() == PARENT_NOT_FOUND
    assert "For my eyes only" not in response.text
    assert post_count(session) == 1


def test_owner_of_a_private_account_can_reply_to_their_own_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = create(alice_client)

    assert reply(alice_client, post["id"]).status_code == 201


def test_cannot_reply_to_a_post_that_became_private(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    post = create(alice_client)
    assert reply(bob_client, post["id"]).status_code == 201

    alice_client.patch("/users/me", json={"is_private": True})

    assert reply(bob_client, post["id"]).status_code == 404


def test_cannot_reply_to_a_post_of_a_deactivated_account(
    bob_client: TestClient, session: Session
) -> None:
    gone = add_user(session, "gone", active=False)
    post = add_post(session, gone)

    assert reply(bob_client, post.id).status_code == 404
    assert post_count(session) == 1


def test_every_parent_that_cannot_be_replied_to_gives_the_same_answer(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    deleted = add_post(session, alice_account, deleted=True)
    private_user = add_user(session, "private_user")
    make_private(session, private_user)
    private = add_post(session, private_user)
    inactive = add_post(session, add_user(session, "inactive", active=False))

    responses = [
        reply(bob_client, id)
        for id in (uuid.uuid4(), deleted.id, private.id, inactive.id)
    ]

    # A reply attempt cannot be used to find out which ids are posts.
    assert {response.status_code for response in responses} == {404}
    assert len({response.text for response in responses}) == 1
    assert post_count(session) == 3


@pytest.mark.parametrize(
    "parent_post_id",
    ["abc", "1", 1, "", "null", "00000000-0000-0000-0000", ["x"], {"id": "x"}, True],
)
def test_malformed_parent_id_is_rejected(
    alice_client: TestClient, session: Session, parent_post_id: object
) -> None:
    response = alice_client.post(
        "/posts", json={"content": "Hello", "parent_post_id": parent_post_id}
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "parent_post_id"]
    assert post_count(session) == 0


def test_a_users_id_is_not_a_post_to_reply_to(
    alice_client: TestClient, session: Session, bob_account: User
) -> None:
    add_post(session, bob_account)

    assert reply(alice_client, bob_account.id).status_code == 404


def test_reply_requires_authentication(
    alice_client: TestClient, make_client, session: Session
) -> None:
    post = create(alice_client)

    response = reply(make_client(), post["id"])

    assert response.status_code == 401
    assert post_count(session) == 1


def test_unauthenticated_reply_attempt_says_nothing_about_the_parent(
    alice_client: TestClient, make_client
) -> None:
    post = create(alice_client)
    anonymous = make_client()

    answers = [reply(anonymous, post["id"]), reply(anonymous, uuid.uuid4())]

    assert {response.status_code for response in answers} == {401}
    assert answers[0].text == answers[1].text


# --- a reply belongs to whoever wrote it ---------------------------------


def test_reply_belongs_to_the_authenticated_user_not_the_parents_author(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    post = create(alice_client)

    response = bob_client.post(
        "/posts",
        json={
            "content": "Alice never said this",
            "parent_post_id": post["id"],
            "author_id": str(alice_account.id),
        },
    )

    assert response.json()["author"]["username"] == "bob"
    stored = post_columns(session, uuid.UUID(response.json()["id"]))
    assert stored["author_id"] == bob_account.id


def test_only_its_author_can_edit_or_delete_a_reply(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    created = reply(bob_client, post["id"], "Original").json()

    # The author of the parent has no say over the reply.
    edited = alice_client.patch(f"/posts/{created['id']}", json={"content": "Hacked"})
    deleted = alice_client.delete(f"/posts/{created['id']}")

    assert edited.status_code == deleted.status_code == 403
    stored = post_columns(session, uuid.UUID(created["id"]))
    assert stored["content"] == "Original"
    assert stored["deleted_at"] is None
    assert bob_client.patch(
        f"/posts/{created['id']}", json={"content": "Edited"}
    ).status_code == 200


def test_reply_has_its_own_edit_window(
    alice_client: TestClient, bob_client: TestClient, clock
) -> None:
    post = create(alice_client)  # 12:00

    clock.advance(minutes=50)  # 12:50
    created = reply(bob_client, post["id"]).json()

    clock.advance(minutes=50)  # 13:40: the parent is 100 minutes old, the reply 50
    assert alice_client.patch(
        f"/posts/{post['id']}", json={"content": "Edited"}
    ).status_code == 409
    assert bob_client.patch(
        f"/posts/{created['id']}", json={"content": "Edited"}
    ).status_code == 200

    clock.advance(minutes=10)  # 13:50: the reply is 60 minutes old
    assert bob_client.patch(
        f"/posts/{created['id']}", json={"content": "Too late"}
    ).status_code == 409


def test_reply_to_an_old_post_is_allowed_and_editable(
    alice_client: TestClient, bob_client: TestClient, clock
) -> None:
    post = create(alice_client)
    clock.advance(days=30)

    created = reply(bob_client, post["id"])

    assert created.status_code == 201
    assert bob_client.patch(
        f"/posts/{created.json()['id']}", json={"content": "Edited"}
    ).status_code == 200


def test_reply_is_deleted_like_any_post(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = create(alice_client)
    created = reply(bob_client, post["id"]).json()

    assert bob_client.delete(f"/posts/{created['id']}").status_code == 204

    assert bob_client.get(f"/posts/{created['id']}").status_code == 404
    assert post_columns(session, uuid.UUID(created["id"]))["deleted_at"] is not None
    assert post_count(session) == 2
    # And a deleted reply cannot be replied to.
    assert reply(alice_client, created["id"]).status_code == 404


# --- whose privacy governs a reply ---------------------------------------


def test_reply_by_a_private_account_to_a_public_post_stays_private(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    bob_account: User,
) -> None:
    post = create(alice_client, "Public post")
    make_private(session, bob_account)

    created = reply(bob_client, post["id"], "Private reply")

    assert created.status_code == 201
    reply_id = created.json()["id"]
    # Not even the author of the post that was replied to can read it.
    for viewer in (make_client(), alice_client):
        response = viewer.get(f"/posts/{reply_id}")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert bob_client.get(f"/posts/{reply_id}").status_code == 200


def test_private_reply_cannot_be_replied_to_by_others(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    bob_account: User,
) -> None:
    post = create(alice_client)
    make_private(session, bob_account)
    private_reply = reply(bob_client, post["id"]).json()

    assert reply(alice_client, private_reply["id"]).status_code == 404
    assert reply(bob_client, private_reply["id"]).status_code == 201


def test_public_reply_stays_visible_when_its_parent_becomes_private(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = create(alice_client)
    created = reply(bob_client, post["id"]).json()

    alice_client.patch("/users/me", json={"is_private": True})
    anonymous = make_client()

    # The reply is bob's and bob is public. The parent is alice's and hidden.
    assert anonymous.get(f"/posts/{created['id']}").status_code == 200
    assert anonymous.get(f"/posts/{post['id']}").status_code == 404


def test_reply_does_not_carry_its_parents_content(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = create(alice_client, "Soon to be hidden")
    created = reply(bob_client, post["id"], "Reply").json()
    alice_client.patch("/users/me", json={"is_private": True})

    response = make_client().get(f"/posts/{created['id']}")

    # Only the parent's id, which opens nothing for someone who cannot see it.
    assert "Soon to be hidden" not in response.text
    assert "alice" not in response.text


def test_private_accounts_see_only_their_own_side_of_a_conversation(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    make_private(session, alice_account)
    make_private(session, bob_account)
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    post = create(alice)

    assert reply(bob, post["id"]).status_code == 404
    assert reply(alice, post["id"]).status_code == 201
