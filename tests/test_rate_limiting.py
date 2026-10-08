"""Rate limiting of the authentication endpoints, and the cooldown on emailed
links.

The counters are rows in PostgreSQL. Most of these tests run in the test's
transaction like every other; the two at the end use real connections and
real commits, because what they are about is what happens between
transactions.
"""

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token
from app.db.session import SessionLocal
from app.main import app
from app.models import (
    EmailVerificationToken,
    PasswordResetToken,
    RateLimit,
    User,
    UserSession,
)
from app.services import auth as auth_service
from app.services import rate_limit
from app.services.rate_limit import Limit, RateLimitedError, key_hash
from helpers import (
    PASSWORD,
    PASSWORD_HASH,
    add_user,
    let_cooldown_pass,
    log_in,
    registration,
    token_from,
)

RATE_LIMITED = {"detail": "Too many requests. Try again later."}
NEW_PASSWORD = "an entirely different passphrase"
WINDOW = timedelta(minutes=settings.rate_limit_window_minutes)
ADDRESS = "203.0.113.7"
OTHER_ADDRESS = "198.51.100.23"


@pytest.fixture
def at(make_client: Callable[..., TestClient]) -> Callable[[str], TestClient]:
    """Creates clients that connect from a given address."""

    def client_at(address: str) -> TestClient:
        return make_client(client=(address, 50000))

    return client_at


@pytest.fixture
def pass_time(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Moves the clock that the limits and the tokens go by forward."""
    offset = timedelta()

    def now() -> datetime:
        return datetime.now(timezone.utc) + offset

    def forward(**duration: float) -> None:
        nonlocal offset
        offset += timedelta(**duration)

    monkeypatch.setattr(rate_limit, "_now", now)
    monkeypatch.setattr(auth_service, "_now", now)
    return forward


def counters(session: Session) -> dict[str, int]:
    return dict(session.execute(select(RateLimit.key_hash, RateLimit.count)).all())


def wrong(client: TestClient, identifier: str = "alice"):
    return log_in(client, identifier, "not the password at all")


def forgot(client: TestClient, email: str = "alice@example.com"):
    return client.post("/auth/forgot-password", json={"email": email})


def resend(client: TestClient, email: str = "alice@example.com"):
    return client.post("/auth/resend-verification", json={"email": email})


# --- the counter ---------------------------------------------------------


def test_counter_allows_the_maximum_and_refuses_the_next(session: Session) -> None:
    limit = Limit("test", "subject", 3)

    for _ in range(3):
        rate_limit.count(session, limit)

    with pytest.raises(RateLimitedError) as refused:
        rate_limit.count(session, limit)
    assert 0 < refused.value.retry_after <= WINDOW.total_seconds()


def test_counter_is_one_row_however_often_it_is_counted(session: Session) -> None:
    limit = Limit("test", "subject", 100)

    for _ in range(7):
        rate_limit.count(session, limit)

    assert counters(session) == {key_hash("test", "subject"): 7}


def test_counter_starts_again_when_the_window_is_over(
    session: Session, pass_time
) -> None:
    limit = Limit("test", "subject", 2)
    for _ in range(2):
        rate_limit.count(session, limit)
    with pytest.raises(RateLimitedError):
        rate_limit.count(session, limit)

    pass_time(minutes=settings.rate_limit_window_minutes - 1)
    with pytest.raises(RateLimitedError):
        rate_limit.count(session, limit)

    pass_time(minutes=1)
    rate_limit.count(session, limit)
    # Still one row: the new window took the place of the old one.
    assert counters(session) == {key_hash("test", "subject"): 1}


def test_being_refused_does_not_extend_the_window(session: Session, pass_time) -> None:
    limit = Limit("test", "subject", 1)
    rate_limit.count(session, limit)

    # Hammering on for the whole window.
    for _ in range(14):
        pass_time(minutes=1)
        with pytest.raises(RateLimitedError):
            rate_limit.count(session, limit)

    pass_time(minutes=1)
    rate_limit.count(session, limit)


def test_retry_after_is_the_time_left_in_the_window(
    session: Session, pass_time
) -> None:
    limit = Limit("test", "subject", 1)
    rate_limit.count(session, limit)
    pass_time(minutes=10)

    with pytest.raises(RateLimitedError) as refused:
        rate_limit.count(session, limit)

    left = WINDOW.total_seconds() - 600
    assert left - 2 <= refused.value.retry_after <= left


def test_counters_are_separate_by_scope_and_by_subject(session: Session) -> None:
    rate_limit.count(session, Limit("one", "subject", 1))

    rate_limit.count(session, Limit("two", "subject", 1))
    rate_limit.count(session, Limit("one", "other", 1))
    with pytest.raises(RateLimitedError):
        rate_limit.count(session, Limit("one", "subject", 1))
    # The scope is part of the key, not a prefix that a subject could fake.
    assert key_hash("one", "subject") != key_hash("on", "esubject")
    assert len(counters(session)) == 3


def test_counting_several_limits_stops_at_the_first_that_is_full(
    session: Session,
) -> None:
    narrow, wide = Limit("narrow", "x", 1), Limit("wide", "x", 10)
    rate_limit.count_all(session, [narrow, wide])

    with pytest.raises(RateLimitedError):
        rate_limit.count_all(session, [narrow, wide])

    # The refused request was not counted against the limit behind.
    assert counters(session)[key_hash("wide", "x")] == 1


def test_refused_request_takes_no_place_in_the_counter(session: Session) -> None:
    limit = Limit("test", "subject", 3)
    for _ in range(3):
        rate_limit.count(session, limit)

    for _ in range(10):
        with pytest.raises(RateLimitedError):
            rate_limit.count(session, limit)

    # Never more than the maximum, however often it is asked.
    assert counters(session) == {key_hash("test", "subject"): 3}


def test_request_refused_by_a_later_limit_gives_back_the_earlier_places(
    session: Session,
) -> None:
    first, second, full = (
        Limit("first", "x", 5),
        Limit("second", "x", 5),
        Limit("full", "x", 1),
    )
    rate_limit.count(session, full)
    rate_limit.count(session, first)

    with pytest.raises(RateLimitedError):
        rate_limit.count_all(session, [first, second, full])

    # Refused by the last: it counts against none of the three, and what
    # was there before it is untouched.
    assert counters(session) == {
        key_hash("first", "x"): 1,
        key_hash("second", "x"): 0,
        key_hash("full", "x"): 1,
    }


def test_a_place_can_be_given_back_once_and_not_below_zero(session: Session) -> None:
    limit = Limit("test", "subject", 2)
    reservation = rate_limit.count(session, limit)

    rate_limit.uncount_all(session, [reservation])
    rate_limit.uncount_all(session, [reservation])

    assert counters(session) == {key_hash("test", "subject"): 0}
    assert reservation.limit == limit


def test_giving_back_a_place_leaves_the_others_in_the_counter(
    session: Session,
) -> None:
    limit = Limit("test", "subject", 5)
    reservations = [rate_limit.count(session, limit) for _ in range(4)]

    rate_limit.uncount_all(session, reservations[:1])

    assert counters(session) == {key_hash("test", "subject"): 3}


def test_a_place_is_not_given_back_in_a_later_window(
    session: Session, pass_time
) -> None:
    limit = Limit("test", "subject", 5)
    # An attempt that began at the very end of a window...
    early = rate_limit.count(session, limit)
    pass_time(minutes=settings.rate_limit_window_minutes)
    # ...while two others were counted in the window after it.
    later = [rate_limit.count(session, limit) for _ in range(2)]

    rate_limit.uncount_all(session, [early])

    # Those two are other requests' and stay. Their own can still go.
    assert counters(session) == {key_hash("test", "subject"): 2}
    assert later[0].window_start != early.window_start
    rate_limit.uncount_all(session, later[:1])
    assert counters(session) == {key_hash("test", "subject"): 1}


def test_count_stands_even_if_the_request_is_rolled_back(session: Session) -> None:
    limit = Limit("test", "subject", 1)
    rate_limit.count(session, limit)

    # What a failing request does with everything it has not committed.
    session.rollback()

    assert counters(session) == {key_hash("test", "subject"): 1}
    with pytest.raises(RateLimitedError):
        rate_limit.count(session, limit)


# --- what is stored ------------------------------------------------------


def test_key_is_a_keyed_hash_and_not_a_plain_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = key_hash("login:address", ADDRESS)

    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key == key_hash("login:address", ADDRESS)
    for guess in (ADDRESS, f"login:address\x00{ADDRESS}", f"login:address{ADDRESS}"):
        assert key != hashlib.sha256(guess.encode()).hexdigest()

    # Without the secret the same address gives another key altogether.
    monkeypatch.setattr(
        settings, "rate_limit_secret", type(settings.rate_limit_secret)("x" * 40)
    )
    assert key_hash("login:address", ADDRESS) != key


def test_table_holds_a_hash_a_time_and_a_number_and_nothing_else() -> None:
    columns = {column.key for column in RateLimit.__table__.columns}

    assert columns == {"key_hash", "window_start", "count"}
    assert [column.key for column in inspect(RateLimit).primary_key] == ["key_hash"]


def test_no_address_identifier_or_email_is_stored(
    at, session: Session, alice_account: User
) -> None:
    client = at(ADDRESS)
    wrong(client, "alice")
    wrong(client, "alice@example.com")
    wrong(client, "someone_else")
    client.post(
        "/auth/register",
        json=registration(username="carol", email="carol@example.com"),
    )
    forgot(client, "alice@example.com")
    resend(client, "carol@example.com")
    client.post("/auth/verify-email", json={"token": "x"})
    client.post(
        "/auth/reset-password", json={"token": "x", "new_password": NEW_PASSWORD}
    )

    rows = session.execute(select(RateLimit.__table__)).all()
    assert len(rows) >= 8
    stored = " ".join(str(value) for row in rows for value in row)
    for raw in (ADDRESS, "alice", "example.com", "someone_else", "carol"):
        assert raw not in stored
    plain_digests = {
        hashlib.sha256(raw.encode()).hexdigest()
        for raw in (ADDRESS, "alice", "alice@example.com", f"alice\x00{ADDRESS}")
    }
    for row in rows:
        assert re.fullmatch(r"[0-9a-f]{64}", row.key_hash)
        assert row.key_hash not in plain_digests
    # The cooldown on links is read from the tokens: an email address is
    # never a key at all.
    assert key_hash("forgot-password", "alice@example.com") not in stored


# --- login ---------------------------------------------------------------


def test_login_is_refused_after_the_allowed_number_of_failures(
    at, alice_account: User
) -> None:
    client = at(ADDRESS)
    allowed = settings.login_failures_per_identifier_and_ip

    failures = [wrong(client) for _ in range(allowed)]
    refused = wrong(client)

    # Exactly at the threshold: every allowed attempt was really checked.
    assert [response.status_code for response in failures] == [401] * allowed
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED


def test_correct_password_is_refused_too_while_the_limit_is_reached(
    at, session: Session, alice_account: User
) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(client)

    response = log_in(client)

    assert response.status_code == 429
    assert "set-cookie" not in response.headers
    assert session.scalars(select(UserSession)).all() == []


def test_refusal_says_when_to_come_back(at, alice_account: User) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(client)

    response = wrong(client)

    assert response.headers["retry-after"].isdigit()
    assert 0 < int(response.headers["retry-after"]) <= WINDOW.total_seconds()
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"


def test_frontend_can_read_when_to_come_back(at, alice_account: User) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(client)

    # As the browser sends it from the frontend's pages.
    response = client.post(
        "/auth/login",
        json={"identifier": "alice", "password": PASSWORD},
        headers={"Origin": settings.frontend_origin},
    )

    assert response.status_code == 429
    assert response.headers["retry-after"].isdigit()
    assert response.headers["access-control-allow-origin"] == settings.frontend_origin
    # Retry-After is not among the headers a browser lets a script of
    # another origin read by default. It has to be named, and it is the
    # only one that is.
    assert response.headers["access-control-expose-headers"] == "Retry-After"


def test_other_origins_are_given_nothing_by_the_exposed_header(
    at, alice_account: User
) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(client)

    response = client.post(
        "/auth/login",
        json={"identifier": "alice", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )

    # Refused as a cross-site request before anything else, and without
    # permission to read the answer there is nothing in it for that
    # origin's scripts, whatever headers are named.
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert "retry-after" not in response.headers


def test_no_request_header_was_opened_up_with_it(at) -> None:
    client = at(ADDRESS)

    def preflight(requested: str):
        return client.options(
            "/auth/login",
            headers={
                "Origin": settings.frontend_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": requested,
            },
        )

    # What the frontend may send is what it was: Content-Type and no more.
    assert preflight("content-type").status_code == 200
    assert preflight("retry-after").status_code == 400
    assert preflight("authorization").status_code == 400


def test_login_succeeds_below_the_threshold(at, alice_account: User) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip - 1):
        wrong(client)

    assert log_in(client).status_code == 200


def test_login_is_possible_again_when_the_window_is_over(
    at, alice_account: User, pass_time
) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(client)
    assert log_in(client).status_code == 429

    pass_time(minutes=settings.rate_limit_window_minutes - 1)
    assert log_in(client).status_code == 429

    pass_time(minutes=1)
    assert wrong(client).status_code == 401
    assert log_in(client).status_code == 200


def test_successful_logins_are_not_counted(
    at, session: Session, alice_account: User
) -> None:
    client = at(ADDRESS)

    for _ in range(settings.login_failures_per_identifier_and_ip * 2):
        assert log_in(client).status_code == 200

    assert set(counters(session).values()) == {0}


def test_wrong_password_is_counted_against_each_of_the_three_limits(
    at, session: Session, alice_account: User
) -> None:
    client = at(ADDRESS)

    wrong(client)
    wrong(client)
    log_in(client)

    assert counters(session) == {
        key_hash("login:identifier+address", f"alice\x00{ADDRESS}"): 2,
        key_hash("login:address", ADDRESS): 2,
        key_hash("login:identifier", "alice"): 2,
    }


def test_refused_login_is_counted_against_nothing(
    at, session: Session, alice_account: User
) -> None:
    client = at(ADDRESS)
    allowed = settings.login_failures_per_identifier_and_ip
    for _ in range(allowed):
        wrong(client)
    before = counters(session)

    # Refused, with the wrong password and with the right one.
    for _ in range(6):
        assert wrong(client).status_code == 429
        assert log_in(client).status_code == 429

    # No password was looked at, so nothing was a failure: the counters
    # hold the wrong passwords and nothing else.
    assert counters(session) == before
    assert set(before.values()) == {allowed}


def test_login_refused_for_its_address_leaves_the_accounts_counters_alone(
    at, session: Session, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_failures_per_ip", 2)
    sprayer = at(ADDRESS)
    wrong(sprayer, "someone")
    wrong(sprayer, "someone_else")

    assert wrong(sprayer, "alice").status_code == 429
    assert log_in(sprayer, "alice").status_code == 429

    # The place taken for alice at this address was given back when the
    # address turned out to be full, and alice's own counter was never
    # reached.
    stored = counters(session)
    assert stored[key_hash("login:identifier+address", f"alice\x00{ADDRESS}")] == 0
    assert key_hash("login:identifier", "alice") not in stored
    assert stored[key_hash("login:address", ADDRESS)] == 2
    assert log_in(at(OTHER_ADDRESS)).status_code == 200


def test_login_refused_for_its_account_leaves_the_addresss_counters_alone(
    at, session: Session, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_failures_per_identifier", 2)
    wrong(at("192.0.2.1"))
    wrong(at("192.0.2.2"))

    # The owner, from an address of their own, while the account is locked.
    owner = at(OTHER_ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip * 2):
        assert log_in(owner).status_code == 429

    # Trying did not use up anything of the owner's own: when the account's
    # window is over they are not locked out by their own attempts.
    stored = counters(session)
    pair = key_hash("login:identifier+address", f"alice\x00{OTHER_ADDRESS}")
    assert stored[pair] == 0
    assert stored[key_hash("login:address", OTHER_ADDRESS)] == 0
    assert stored[key_hash("login:identifier", "alice")] == 2
    monkeypatch.setattr(settings, "login_failures_per_identifier", 20)
    assert log_in(owner).status_code == 200


def test_success_does_not_wipe_out_the_failures_before_it(
    at, alice_account: User
) -> None:
    client = at(ADDRESS)
    allowed = settings.login_failures_per_identifier_and_ip
    for _ in range(allowed - 1):
        wrong(client)
    assert log_in(client).status_code == 200

    # One guess is left in the window, not a fresh set of them: logging in
    # to an account one knows the password of buys no guesses.
    assert wrong(client).status_code == 401
    assert wrong(client).status_code == 429


def test_password_is_not_checked_once_the_limit_is_reached(
    at, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = at(ADDRESS)
    checked = []
    real_verify = auth_service.verify_password

    def recording_verify(password: str, password_hash: str) -> bool:
        checked.append(password)
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password", recording_verify)
    allowed = settings.login_failures_per_identifier_and_ip
    for _ in range(allowed):
        wrong(client)
    assert len(checked) == allowed

    for _ in range(5):
        assert log_in(client).status_code == 429

    # Refused before the expensive part, and before the account is read.
    assert len(checked) == allowed


def test_unknown_identifier_is_limited_exactly_like_a_known_one(
    at, alice_account: User
) -> None:
    known, unknown = at(ADDRESS), at(OTHER_ADDRESS)
    allowed = settings.login_failures_per_identifier_and_ip

    for_known = [wrong(known, "alice") for _ in range(allowed + 1)]
    for_unknown = [wrong(unknown, "nobody_here") for _ in range(allowed + 1)]

    def shape(response) -> tuple:
        return (response.status_code, response.text, tuple(sorted(response.headers)))

    assert [shape(r) for r in for_known] == [shape(r) for r in for_unknown]
    assert [r.status_code for r in for_known] == [401] * allowed + [429]


def test_one_address_does_not_lock_the_account_for_other_addresses(
    at, alice_account: User
) -> None:
    attacker, owner = at(ADDRESS), at(OTHER_ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(attacker)
    assert log_in(attacker).status_code == 429

    assert log_in(owner).status_code == 200


def test_one_address_cannot_use_up_the_accounts_limit_by_carrying_on(
    at, session: Session, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The limit for the account over all addresses, set just above what one
    # address is allowed on its own.
    allowed = settings.login_failures_per_identifier_and_ip
    monkeypatch.setattr(settings, "login_failures_per_identifier", allowed + 1)
    attacker, owner = at(ADDRESS), at(OTHER_ADDRESS)

    for _ in range(allowed * 4):
        wrong(attacker)

    # Refused attempts were not counted against the account, so the
    # account's own limit was never reached and its owner gets in.
    assert counters(session)[key_hash("login:identifier", "alice")] == allowed
    assert log_in(owner).status_code == 200


def test_failures_for_one_account_do_not_block_another(
    at, alice_account: User, bob_account: User
) -> None:
    client = at(ADDRESS)
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(client, "alice")
    assert log_in(client, "alice").status_code == 429

    assert log_in(client, "bob").status_code == 200


def test_guesses_spread_over_addresses_are_stopped_by_the_accounts_limit(
    at, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_failures_per_identifier", 6)
    for number in range(3):
        client = at(f"192.0.2.{number + 1}")
        assert [wrong(client).status_code for _ in range(2)] == [401, 401]

    # A seventh guess, from an address that has made none.
    response = log_in(at("192.0.2.200"))

    assert response.status_code == 429
    assert response.json() == RATE_LIMITED


def test_one_address_trying_many_accounts_is_stopped_by_the_address_limit(
    at, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_failures_per_ip", 6)
    sprayer = at(ADDRESS)
    sprayed = [wrong(sprayer, f"account_{number}") for number in range(6)]
    assert {response.status_code for response in sprayed} == {401}

    assert wrong(sprayer, "yet_another").status_code == 429
    # For any account, also with its password.
    assert log_in(sprayer).status_code == 429
    assert log_in(at(OTHER_ADDRESS)).status_code == 200


def test_every_limit_is_refused_with_the_same_answer(
    at, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(settings.login_failures_per_identifier_and_ip):
        wrong(at(ADDRESS))
    by_pair = wrong(at(ADDRESS))

    monkeypatch.setattr(settings, "login_failures_per_ip", 1)
    wrong(at("192.0.2.50"), "first")
    by_address = wrong(at("192.0.2.50"), "second")

    monkeypatch.setattr(settings, "login_failures_per_identifier", 1)
    wrong(at("192.0.2.60"), "third")
    by_identifier = wrong(at("192.0.2.61"), "third")

    answers = [by_pair, by_address, by_identifier]
    assert {response.status_code for response in answers} == {429}
    # Nothing says which of the limits it was.
    assert len({response.text for response in answers}) == 1
    assert len({tuple(sorted(response.headers)) for response in answers}) == 1


def test_right_password_of_an_unverified_account_is_not_a_failure(
    at, session: Session
) -> None:
    add_user(session, "carol", verified=False)
    client = at(ADDRESS)

    answers = [
        log_in(client, "carol").status_code
        for _ in range(settings.login_failures_per_identifier_and_ip + 2)
    ]

    assert set(answers) == {403}
    assert set(counters(session).values()) == {0}


def test_invalid_login_request_is_not_counted(at, session: Session) -> None:
    client = at(ADDRESS)

    for _ in range(10):
        assert client.post("/auth/login", json={"identifier": ""}).status_code == 422

    # Nothing was tried, so there is nothing to count.
    assert counters(session) == {}


def test_limits_are_read_from_the_settings(
    at, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_failures_per_identifier_and_ip", 2)
    client = at(ADDRESS)

    assert [wrong(client).status_code for _ in range(3)] == [401, 401, 429]


# --- registration --------------------------------------------------------


def test_registration_is_limited_per_address(at, session: Session) -> None:
    client = at(ADDRESS)
    allowed = settings.registrations_per_ip

    created = [
        client.post(
            "/auth/register",
            json=registration(username=f"user{n}", email=f"user{n}@example.com"),
        )
        for n in range(allowed)
    ]
    refused = client.post(
        "/auth/register",
        json=registration(username="onemore", email="onemore@example.com"),
    )

    assert [response.status_code for response in created] == [201] * allowed
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED
    assert refused.headers["retry-after"].isdigit()
    assert session.scalar(select(func.count()).select_from(User)) == allowed


def test_registration_limit_counts_requests_whatever_their_outcome(
    at, alice_account: User
) -> None:
    client = at(ADDRESS)
    answers = [
        client.post("/auth/register", json=registration()),  # taken
        client.post("/auth/register", json=registration(password="short")),
        client.post("/auth/register", json={}),
        client.post("/auth/register", json=registration(username="alice")),
        client.post("/auth/register", json=registration(email="alice@example.com")),
        client.post("/auth/register", json=registration(username="new_name")),
    ]

    # Asking which names and addresses are taken is limited like everything
    # else that is asked of this endpoint.
    assert [r.status_code for r in answers] == [409, 422, 422, 409, 409, 429]


def test_registration_limit_is_per_address_and_not_per_email(
    at, session: Session
) -> None:
    for number in range(settings.registrations_per_ip):
        at(ADDRESS).post(
            "/auth/register",
            json=registration(username=f"user{number}", email=f"u{number}@example.com"),
        )
    payload = registration(username="carol", email="carol@example.com")

    assert at(ADDRESS).post("/auth/register", json=payload).status_code == 429
    # The same person from elsewhere is not held to it.
    assert at(OTHER_ADDRESS).post("/auth/register", json=payload).status_code == 201


def test_registration_is_possible_again_when_the_window_is_over(
    at, pass_time
) -> None:
    client = at(ADDRESS)
    for _ in range(settings.registrations_per_ip + 1):
        client.post("/auth/register", json={})
    payload = registration()
    assert client.post("/auth/register", json=payload).status_code == 429

    pass_time(minutes=settings.rate_limit_window_minutes)

    assert client.post("/auth/register", json=payload).status_code == 201


# --- the other endpoints, per address ------------------------------------


def test_forgot_password_is_limited_per_address(
    at, alice_account: User, outbox
) -> None:
    client = at(ADDRESS)
    allowed = settings.email_requests_per_ip

    accepted = [forgot(client, f"someone{n}@example.com") for n in range(allowed)]
    refused = forgot(client)

    assert [response.status_code for response in accepted] == [202] * allowed
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED
    assert refused.headers["retry-after"].isdigit()
    # Refused before the address was even looked up.
    assert outbox.password_reset == []
    assert forgot(at(OTHER_ADDRESS)).status_code == 202
    assert len(outbox.password_reset) == 1


def test_resend_verification_is_limited_per_address(
    at, session: Session, outbox
) -> None:
    add_user(session, "carol", verified=False)
    client = at(ADDRESS)
    allowed = settings.email_requests_per_ip

    accepted = [resend(client, f"someone{n}@example.com") for n in range(allowed)]
    refused = resend(client, "carol@example.com")

    assert [response.status_code for response in accepted] == [202] * allowed
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED
    assert refused.headers["retry-after"].isdigit()
    assert outbox.verification == []
    assert resend(at(OTHER_ADDRESS), "carol@example.com").status_code == 202
    assert len(outbox.verification) == 1


def test_verify_email_is_limited_per_address(at, session: Session, outbox) -> None:
    client = at(ADDRESS)
    client.post("/auth/register", json=registration())
    token = token_from(outbox.verification[0][1])
    allowed = settings.token_redemptions_per_ip

    guesses = [
        client.post("/auth/verify-email", json={"token": f"guess-{n}"})
        for n in range(allowed)
    ]
    refused = client.post("/auth/verify-email", json={"token": token})

    assert [response.status_code for response in guesses] == [400] * allowed
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED
    assert refused.headers["retry-after"].isdigit()
    # The genuine token was not used up by the refusal. It is still good
    # from an address that is not limited, once.
    other = at(OTHER_ADDRESS)
    assert other.post("/auth/verify-email", json={"token": token}).status_code == 200
    assert other.post("/auth/verify-email", json={"token": token}).status_code == 400


def test_reset_password_is_limited_per_address(
    at, session: Session, alice_account: User, outbox, pass_time
) -> None:
    client = at(ADDRESS)
    forgot(at(OTHER_ADDRESS))
    token = token_from(outbox.password_reset[0][1])
    allowed = settings.token_redemptions_per_ip

    def reset(with_token: str):
        return client.post(
            "/auth/reset-password",
            json={"token": with_token, "new_password": NEW_PASSWORD},
        )

    guesses = [reset(f"guess-{n}") for n in range(allowed)]
    refused = reset(token)

    assert [response.status_code for response in guesses] == [400] * allowed
    assert refused.status_code == 429
    assert refused.json() == RATE_LIMITED
    assert refused.headers["retry-after"].isdigit()
    # Nothing came of the refused request: the password is the old one and
    # the token is unused.
    assert log_in(at(OTHER_ADDRESS)).status_code == 200
    assert session.scalars(select(PasswordResetToken.used_at)).all() == [None]

    # The window is shorter than the token's life, so it can still be used.
    pass_time(minutes=settings.rate_limit_window_minutes)
    assert reset(token).status_code == 200
    assert reset(token).status_code == 400
    assert log_in(at(OTHER_ADDRESS), "alice", NEW_PASSWORD).status_code == 200


def test_each_endpoint_has_a_counter_of_its_own(
    at, session: Session, alice_account: User
) -> None:
    client = at(ADDRESS)
    for _ in range(settings.email_requests_per_ip + 1):
        forgot(client, "nobody@example.com")
    assert forgot(client).status_code == 429

    assert resend(client).status_code == 202
    assert client.post("/auth/verify-email", json={"token": "x"}).status_code == 400
    assert log_in(client).status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_reading_is_not_rate_limited(at, session: Session, alice_account: User) -> None:
    client = at(ADDRESS)
    log_in(client)

    for _ in range(40):
        assert client.get("/auth/me").status_code == 200
        assert client.get("/feed").status_code == 200

    assert set(counters(session).values()) == {0}


# --- the cooldown on password reset links --------------------------------


def test_second_reset_request_within_the_cooldown_issues_no_new_token(
    client: TestClient, session: Session, alice_account: User, outbox
) -> None:
    forgot(client)
    first = token_from(outbox.password_reset[0][1])

    forgot(client)
    forgot(client)

    assert len(outbox.password_reset) == 1
    assert session.scalars(select(PasswordResetToken.token_hash)).all() == [
        hash_token(first)
    ]


def test_reset_link_survives_further_requests_within_the_cooldown(
    client: TestClient, alice_account: User, outbox
) -> None:
    forgot(client)
    token = token_from(outbox.password_reset[0][1])

    # Someone else who knows the address asks again and again.
    for _ in range(3):
        forgot(client)

    response = client.post(
        "/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert response.status_code == 200
    assert log_in(client, "alice", NEW_PASSWORD).status_code == 200


def test_reset_request_is_answered_alike_inside_and_outside_the_cooldown(
    client: TestClient, session: Session, alice_account: User, outbox
) -> None:
    add_user(session, "inactive", active=False)

    outside = forgot(client)
    inside = forgot(client)
    unknown = forgot(client, "nobody@example.com")
    deactivated = forgot(client, "inactive@example.com")

    answers = [outside, inside, unknown, deactivated]
    assert {response.status_code for response in answers} == {202}
    assert len({response.text for response in answers}) == 1
    assert len({tuple(sorted(response.headers)) for response in answers}) == 1
    assert len(outbox.password_reset) == 1


def test_new_reset_link_can_be_requested_once_the_cooldown_is_over(
    client: TestClient, alice_account: User, outbox, pass_time
) -> None:
    forgot(client)
    pass_time(seconds=settings.email_token_cooldown_seconds - 5)
    forgot(client)
    assert len(outbox.password_reset) == 1

    pass_time(seconds=6)
    forgot(client)

    assert len(outbox.password_reset) == 2
    first, second = (token_from(url) for _, url in outbox.password_reset)

    def reset(token: str) -> int:
        return client.post(
            "/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
        ).status_code

    # Only the latest link works, as before.
    assert reset(first) == 400
    assert reset(second) == 200


def test_cooldown_ends_when_the_link_has_been_used(
    client: TestClient, alice_account: User, outbox
) -> None:
    forgot(client)
    token = token_from(outbox.password_reset[0][1])
    client.post(
        "/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
    )

    forgot(client)

    # There is no link left to protect, so a new one is sent at once.
    assert len(outbox.password_reset) == 2


def test_cooldown_is_each_accounts_own(
    client: TestClient, alice_account: User, bob_account: User, outbox
) -> None:
    forgot(client, "alice@example.com")
    forgot(client, "bob@example.com")
    forgot(client, "alice@example.com")

    assert [to for to, _ in outbox.password_reset] == [
        "alice@example.com",
        "bob@example.com",
    ]


def test_cooldown_can_be_switched_off(
    client: TestClient, alice_account: User, outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "email_token_cooldown_seconds", 0)

    forgot(client)
    forgot(client)

    assert len(outbox.password_reset) == 2


def test_no_reset_token_is_in_a_response_inside_or_outside_the_cooldown(
    client: TestClient, alice_account: User, outbox
) -> None:
    answers = [forgot(client), forgot(client)]
    token = token_from(outbox.password_reset[0][1])

    for response in answers:
        assert token not in response.text
        assert hash_token(token) not in response.text
        assert set(response.json()) == {"message"}


# --- the cooldown on verification links ----------------------------------


def test_resend_within_the_cooldown_keeps_the_link_from_registration(
    client: TestClient, session: Session, outbox
) -> None:
    client.post("/auth/register", json=registration())
    first = token_from(outbox.verification[0][1])

    for _ in range(3):
        assert resend(client).status_code == 202

    assert len(outbox.verification) == 1
    assert session.scalars(select(EmailVerificationToken.token_hash)).all() == [
        hash_token(first)
    ]
    assert client.post("/auth/verify-email", json={"token": first}).status_code == 200


def test_resend_is_answered_alike_inside_and_outside_the_cooldown(
    client: TestClient, session: Session, outbox
) -> None:
    client.post("/auth/register", json=registration())
    add_user(session, "verified")
    inside = resend(client)
    let_cooldown_pass(session)
    outside = resend(client)
    unknown = resend(client, "nobody@example.com")
    verified = resend(client, "verified@example.com")

    answers = [inside, outside, unknown, verified]
    assert {response.status_code for response in answers} == {202}
    assert len({response.text for response in answers}) == 1
    assert len({tuple(sorted(response.headers)) for response in answers}) == 1
    # One from registering and one from the resend after the cooldown.
    assert len(outbox.verification) == 2


def test_new_verification_link_can_be_requested_once_the_cooldown_is_over(
    client: TestClient, outbox, pass_time
) -> None:
    client.post("/auth/register", json=registration())
    resend(client)
    assert len(outbox.verification) == 1

    pass_time(seconds=settings.email_token_cooldown_seconds + 1)
    resend(client)

    first, second = (token_from(url) for _, url in outbox.verification)
    assert client.post("/auth/verify-email", json={"token": first}).status_code == 400
    assert client.post("/auth/verify-email", json={"token": second}).status_code == 200


def test_cooldowns_of_the_two_kinds_of_link_are_separate(
    client: TestClient, session: Session, outbox
) -> None:
    client.post("/auth/register", json=registration())

    # A verification link was just issued. That does not hold back a reset.
    forgot(client)

    assert len(outbox.password_reset) == 1


# --- between transactions ------------------------------------------------
#
# These commit for real, on connections of their own, and remove what they
# wrote. Each works on an account and on addresses that nothing else uses.

LOGIN_SCOPES = ("login:identifier+address", "login:address", "login:identifier")
ADDRESS_SCOPES = (
    "login:address",
    "forgot-password",
    "resend-verification",
    "verify-email",
    "reset-password",
)


class Committed:
    """An account that really exists, with the addresses used against it."""

    def __init__(self, *, verified: bool = True) -> None:
        self.name = f"race_{uuid.uuid4().hex[:12]}"
        self.email = f"{self.name}@example.com"
        self.run = uuid.uuid4().hex
        self.addresses: set[str] = set()
        with SessionLocal() as db:
            user = User(
                username=self.name,
                email=self.email,
                password_hash=PASSWORD_HASH,
                display_name="Race",
                email_verified_at=datetime.now(timezone.utc) if verified else None,
            )
            db.add(user)
            db.commit()
            self.id = user.id

    def address(self, number: int = 0) -> str:
        address = f"race-{self.run}-{number}"
        self.addresses.add(address)
        return address

    def client(self, number: int = 0) -> TestClient:
        # The application as it really runs: its own database sessions.
        return TestClient(
            app, base_url="https://testserver", client=(self.address(number), 50000)
        )

    def keys(self) -> list[str]:
        keys = [key_hash("login:identifier", self.name)]
        keys += [key_hash("login:identifier", self.email)]
        for address in self.addresses:
            keys += [key_hash(scope, address) for scope in ADDRESS_SCOPES]
            for identifier in (self.name, self.email):
                keys.append(
                    key_hash("login:identifier+address", f"{identifier}\x00{address}")
                )
        return keys

    def login_counters(self, number: int = 0) -> list[int | None]:
        """The three counters of failed logins by name from one address."""
        address = self.address(number)
        subjects = (f"{self.name}\x00{address}", address, self.name)
        with SessionLocal() as db:
            return [
                db.scalar(
                    select(RateLimit.count).where(
                        RateLimit.key_hash == key_hash(scope, subject)
                    )
                )
                for scope, subject in zip(LOGIN_SCOPES, subjects)
            ]

    def unused_tokens(self, model: type) -> list[str]:
        with SessionLocal() as db:
            return list(
                db.scalars(
                    select(model.token_hash).where(
                        model.user_id == self.id, model.used_at.is_(None)
                    )
                )
            )

    def remove(self) -> None:
        with SessionLocal() as db:
            db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(self.keys())))
            # Its sessions and tokens go with it.
            db.execute(delete(User).where(User.id == self.id))
            db.commit()


@pytest.fixture
def committed() -> Iterator[Callable[..., Committed]]:
    made: list[Committed] = []

    def make(**kwargs: bool) -> Committed:
        made.append(Committed(**kwargs))
        return made[-1]

    yield make
    for account in made:
        account.remove()


@pytest.fixture
def issued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every raw token the application generates, in the order it does."""
    tokens: list[str] = []
    generate = auth_service.generate_token

    def recording_generate() -> str:
        tokens.append(generate())
        return tokens[-1]

    monkeypatch.setattr(auth_service, "generate_token", recording_generate)
    return tokens


def at_once(count: int, request: Callable[[int], int]) -> list[int]:
    """The answers to ``count`` requests that are all sent at the same moment."""
    together = threading.Barrier(count)

    def run(number: int) -> int:
        together.wait()
        return request(number)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(run, range(count)))


def test_concurrent_requests_cannot_get_past_a_counter() -> None:
    limit = Limit("test:concurrency", uuid.uuid4().hex, 5)
    workers = 16

    def attempt(_: int) -> int:
        with SessionLocal() as db:
            try:
                rate_limit.count(db, limit)
            except RateLimitedError:
                return 0
            return 1

    try:
        allowed = at_once(workers, attempt) + at_once(workers, attempt)

        with SessionLocal() as db:
            counted = db.scalar(
                select(RateLimit.count).where(
                    RateLimit.key_hash == key_hash(limit.scope, limit.subject)
                )
            )
        # Exactly as many as the limit got through, of all that arrived at
        # the same moment. Those are counted, each once, and the refused
        # ones are not.
        assert sum(allowed) == 5
        assert counted == 5
    finally:
        with SessionLocal() as db:
            db.execute(
                delete(RateLimit).where(
                    RateLimit.key_hash == key_hash(limit.scope, limit.subject)
                )
            )
            db.commit()


def test_concurrent_takers_and_givers_keep_the_counter_exact() -> None:
    limit = Limit("test:concurrency", uuid.uuid4().hex, 8)
    workers = 24

    def take_and_maybe_give_back(number: int) -> str:
        with SessionLocal() as db:
            try:
                reservation = rate_limit.count(db, limit)
            except RateLimitedError:
                return "refused"
            # Every other one is a success and gives its place back.
            if number % 2:
                rate_limit.uncount_all(db, [reservation])
                return "given back"
            return "kept"

    try:
        outcomes = at_once(workers, take_and_maybe_give_back)

        with SessionLocal() as db:
            counted = db.scalar(
                select(RateLimit.count).where(
                    RateLimit.key_hash == key_hash(limit.scope, limit.subject)
                )
            )
        # However they were interleaved: what is left is what was kept, no
        # place was given back twice or by a request that had none.
        assert counted == outcomes.count("kept")
        assert counted <= 8
        assert outcomes.count("kept") + outcomes.count("given back") >= 8
    finally:
        with SessionLocal() as db:
            db.execute(
                delete(RateLimit).where(
                    RateLimit.key_hash == key_hash(limit.scope, limit.subject)
                )
            )
            db.commit()


def test_concurrent_wrong_logins_get_no_more_guesses_than_the_limit(
    committed,
) -> None:
    account = committed()
    allowed = settings.login_failures_per_identifier_and_ip
    workers = allowed * 3

    def guess(_: int) -> int:
        with account.client() as client:
            return wrong(client, account.name).status_code

    answers = at_once(workers, guess)

    # However they were interleaved, only as many passwords were looked at
    # as one address is allowed for one account.
    assert sorted(answers) == [401] * allowed + [429] * (workers - allowed)
    # The wrong passwords are counted, and the refused requests are not.
    assert account.login_counters() == [allowed] * 3
    with account.client() as client:
        assert log_in(client, account.name).status_code == 429


def test_concurrent_correct_logins_leave_no_failures_behind(committed) -> None:
    account = committed()
    workers = settings.login_failures_per_identifier_and_ip * 3

    def sign_in(_: int) -> int:
        with account.client() as client:
            return log_in(client, account.name).status_code

    answers = at_once(workers, sign_in)

    # Not one wrong password was sent. Some of the requests may have been
    # turned away for the moment, while more were under way than the
    # counter has places, but none of them left anything in a counter...
    assert set(answers) <= {200, 429}
    assert 200 in answers
    assert account.login_counters() == [0, 0, 0]
    # ...so the account is not locked: it can be logged in to, and all the
    # guesses an address is allowed are still there.
    with account.client() as client:
        assert log_in(client, account.name).status_code == 200
        allowed = settings.login_failures_per_identifier_and_ip
        assert [wrong(client, account.name).status_code for _ in range(allowed)] == [
            401
        ] * allowed
    with SessionLocal() as db:
        sessions = db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.user_id == account.id)
        )
    assert sessions == answers.count(200) + 1


def test_concurrent_correct_and_wrong_logins_count_only_the_wrong_ones(
    committed,
) -> None:
    account = committed()
    allowed = settings.login_failures_per_identifier_and_ip
    workers = allowed * 4

    def attempt(number: int) -> tuple[bool, int]:
        correct = number % 2 == 0
        with account.client() as client:
            response = log_in(client, account.name) if correct else wrong(
                client, account.name
            )
            return correct, response.status_code

    outcomes = at_once(workers, attempt)

    right = [status for correct, status in outcomes if correct]
    not_right = [status for correct, status in outcomes if not correct]
    assert set(right) <= {200, 429}
    assert set(not_right) <= {401, 429}
    failures = not_right.count(401)
    # Never more passwords found wrong than the limit, and the counters
    # hold exactly those: no place was kept by a login that succeeded or by
    # a request that was refused, and none of a failure's was given back.
    assert failures <= allowed
    assert account.login_counters() == [failures] * 3
    with SessionLocal() as db:
        sessions = db.scalar(
            select(func.count())
            .select_from(UserSession)
            .where(UserSession.user_id == account.id)
        )
    assert sessions == right.count(200)


# --- links, between transactions -----------------------------------------


def reset_with(account: Committed, token: str, number: int = 99) -> int:
    with account.client(number) as client:
        return client.post(
            "/auth/reset-password",
            json={"token": token, "new_password": NEW_PASSWORD},
        ).status_code


def test_concurrent_reset_requests_issue_one_link_between_them(
    committed, issued: list[str]
) -> None:
    account = committed()
    workers = 8

    def ask(number: int) -> tuple[int, str]:
        # Each from an address of its own: this is not about that limit.
        with account.client(number) as client:
            response = forgot(client, account.email)
            return response.status_code, response.text

    answers = at_once(workers, ask)

    assert {status for status, _ in answers} == {202}
    assert len({text for _, text in answers}) == 1
    # One of them issued a link. The others waited for it and then found
    # it there, so they issued none and withdrew none.
    assert len(issued) == 1
    assert account.unused_tokens(PasswordResetToken) == [hash_token(issued[0])]
    assert reset_with(account, issued[0]) == 200


def test_concurrent_reset_requests_leave_the_link_already_sent_alone(
    committed, issued: list[str]
) -> None:
    account = committed()
    with account.client(100) as client:
        assert forgot(client, account.email).status_code == 202
    [sent] = issued

    def ask(number: int) -> int:
        with account.client(number) as client:
            return forgot(client, account.email).status_code

    for _ in range(2):
        assert set(at_once(8, ask)) == {202}

    # Inside the cooldown, however many ask and however closely together.
    assert issued == [sent]
    assert account.unused_tokens(PasswordResetToken) == [hash_token(sent)]
    assert reset_with(account, sent) == 200


def test_concurrent_resend_requests_issue_one_link_between_them(
    committed, issued: list[str]
) -> None:
    account = committed(verified=False)
    workers = 8

    def ask(number: int) -> tuple[int, str]:
        with account.client(number) as client:
            response = resend(client, account.email)
            return response.status_code, response.text

    answers = at_once(workers, ask)

    assert {status for status, _ in answers} == {202}
    assert len({text for _, text in answers}) == 1
    assert len(issued) == 1
    assert account.unused_tokens(EmailVerificationToken) == [hash_token(issued[0])]
    with account.client(99) as client:
        verified = client.post("/auth/verify-email", json={"token": issued[0]})
        assert verified.status_code == 200
        assert log_in(client, account.name).status_code == 200


def test_concurrent_resend_requests_leave_the_link_already_sent_alone(
    committed, issued: list[str]
) -> None:
    account = committed(verified=False)
    with account.client(100) as client:
        assert resend(client, account.email).status_code == 202
    [sent] = issued

    def ask(number: int) -> int:
        with account.client(number) as client:
            return resend(client, account.email).status_code

    for _ in range(2):
        assert set(at_once(8, ask)) == {202}

    assert issued == [sent]
    assert account.unused_tokens(EmailVerificationToken) == [hash_token(sent)]
    with account.client(99) as client:
        verified = client.post("/auth/verify-email", json={"token": sent})
        assert verified.status_code == 200


def test_requests_for_different_accounts_do_not_wait_for_each_other(
    committed, issued: list[str]
) -> None:
    accounts = [committed() for _ in range(4)]

    def ask(number: int) -> int:
        account = accounts[number]
        with account.client(number) as client:
            return forgot(client, account.email).status_code

    assert set(at_once(4, ask)) == {202}

    # The lock is on the one account asked about: each got its own link.
    assert len(issued) == 4
    for account in accounts:
        assert len(account.unused_tokens(PasswordResetToken)) == 1
