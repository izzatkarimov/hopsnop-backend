"""Cross-cutting protections: CSRF, CORS, what responses and logs may contain."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_token, hash_token
from app.main import app
from app.models import EmailVerificationToken, PasswordResetToken, User, UserSession
from app.services import auth as auth_service
from app.services.email import (
    ConsoleEmailSender,
    DisabledEmailSender,
    get_email_sender,
)
from helpers import (
    PASSWORD,
    SENSITIVE_KEYS,
    keys_in,
    log_in,
    registration,
    session_token,
    token_from,
)

FOREIGN_ORIGIN = "https://evil.example"
OWN_ORIGIN = "https://testserver"
NEW_PASSWORD = "a brand new passphrase"

STATE_CHANGING_ROUTES = sorted(
    (method.upper(), path.replace("{session_id}", str(uuid.uuid4())))
    for path, operations in app.openapi()["paths"].items()
    for method in operations
    if method in {"post", "put", "patch", "delete"}
)


# --- health --------------------------------------------------------------


def test_health_endpoint_still_works(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- CSRF: origin verification -------------------------------------------


def test_there_are_state_changing_routes_to_check() -> None:
    assert ("POST", "/auth/login") in STATE_CHANGING_ROUTES
    assert len(STATE_CHANGING_ROUTES) >= 9


@pytest.mark.parametrize(("method", "path"), STATE_CHANGING_ROUTES)
def test_state_changing_request_from_another_site_is_rejected(
    alice_client: TestClient, method: str, path: str
) -> None:
    response = alice_client.request(method, path, headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request rejected."}


def test_rejected_cross_site_request_has_no_effect(
    alice_client: TestClient, session: Session
) -> None:
    response = alice_client.post("/auth/logout", headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 403
    assert session.scalars(select(UserSession)).one().revoked_at is None
    assert alice_client.get("/auth/me").status_code == 200


def test_cross_site_login_is_rejected(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # Login CSRF: logging the victim's browser into the attacker's account.
    response = client.post(
        "/auth/login",
        json={"identifier": "alice", "password": PASSWORD},
        headers={"Origin": FOREIGN_ORIGIN},
    )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert session.scalars(select(UserSession)).all() == []


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://testserver",  # this host, but not over HTTPS
        "https://testserver.evil.example",
        "https://testserver:8443",
        f"{settings.frontend_origin}.evil.example",
        "",
    ],
)
def test_untrusted_origins_are_rejected(alice_client: TestClient, origin: str) -> None:
    response = alice_client.post("/auth/logout", headers={"Origin": origin})

    assert response.status_code == 403


def test_request_from_the_frontend_origin_is_accepted(alice_client: TestClient) -> None:
    response = alice_client.post(
        "/auth/logout", headers={"Origin": settings.frontend_origin}
    )

    assert response.status_code == 204


def test_frontend_origin_is_compared_case_insensitively(
    alice_client: TestClient,
) -> None:
    response = alice_client.post(
        "/auth/logout", headers={"Origin": settings.frontend_origin.upper()}
    )

    assert response.status_code == 204


def test_same_origin_request_is_accepted(alice_client: TestClient) -> None:
    response = alice_client.post("/auth/logout", headers={"Origin": OWN_ORIGIN})

    assert response.status_code == 204


def test_request_without_origin_or_referer_is_accepted(
    alice_client: TestClient,
) -> None:
    # Not sent by a browser on behalf of another site, so not a CSRF vector.
    request = alice_client.build_request("POST", "/auth/logout")
    assert "origin" not in request.headers and "referer" not in request.headers

    assert alice_client.send(request).status_code == 204


def test_referer_is_checked_when_origin_is_absent(
    make_client, alice_account: User
) -> None:
    foreign, trusted = make_client(), make_client()
    log_in(foreign)
    log_in(trusted)

    rejected = foreign.post(
        "/auth/logout", headers={"Referer": f"{FOREIGN_ORIGIN}/some/page?x=1"}
    )
    accepted = trusted.post(
        "/auth/logout", headers={"Referer": f"{settings.frontend_origin}/settings"}
    )

    assert rejected.status_code == 403
    assert foreign.get("/auth/me").status_code == 200
    assert accepted.status_code == 204


def test_safe_methods_are_not_subject_to_the_origin_check(
    alice_client: TestClient,
) -> None:
    # Reads change nothing; keeping another site from seeing the response is
    # the job of CORS, tested below.
    response = alice_client.get("/auth/me", headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_no_get_route_changes_state() -> None:
    # The origin check and SameSite=Lax both exempt GET, so a GET must never
    # be given side effects.
    get_routes = sorted(
        path
        for path, operations in app.openapi()["paths"].items()
        if "get" in operations
    )

    assert get_routes == [
        "/auth/me",
        "/auth/sessions",
        "/health",
        "/users/me",
        "/users/{username}",
    ]


def test_json_body_must_be_declared_as_json(
    client: TestClient, session: Session
) -> None:
    # A cross-site HTML form can send JSON-looking text without a preflight,
    # but only with a form content type.
    response = client.post(
        "/auth/register",
        content=json.dumps(registration()),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert session.scalars(select(User)).all() == []


# --- CORS ----------------------------------------------------------------


def preflight(client: TestClient, origin: str):
    return client.options(
        "/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_frontend_origin_may_make_credentialed_requests(client: TestClient) -> None:
    response = preflight(client, settings.frontend_origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == settings.frontend_origin
    assert response.headers["access-control-allow-credentials"] == "true"


def test_other_origins_get_no_cors_permission(
    alice_client: TestClient,
) -> None:
    preflighted = preflight(alice_client, FOREIGN_ORIGIN)
    simple = alice_client.get("/auth/me", headers={"Origin": FOREIGN_ORIGIN})

    # Without Access-Control-Allow-Origin the browser refuses the preflighted
    # request and keeps the page from reading the simple one's response. (The
    # middleware's static Allow-Credentials header grants nothing by itself.)
    assert preflighted.status_code == 400
    assert "access-control-allow-origin" not in preflighted.headers
    assert "access-control-allow-origin" not in simple.headers


def test_cors_never_uses_a_wildcard_origin(alice_client: TestClient) -> None:
    response = alice_client.get(
        "/auth/me", headers={"Origin": settings.frontend_origin}
    )

    assert response.headers["access-control-allow-origin"] == settings.frontend_origin
    assert response.headers["access-control-allow-origin"] != "*"


# --- what responses may contain ------------------------------------------


def test_no_response_contains_a_secret(
    make_client, session: Session, outbox
) -> None:
    """Walks through every endpoint and inspects everything that came back."""
    client, second = make_client(), make_client()
    responses = []

    def call(who: TestClient, method: str, path: str, **body: object) -> None:
        responses.append(who.request(method, path, json=body or None))

    login, reset = "/auth/login", "/auth/reset-password"
    call(client, "POST", "/auth/register", **registration())
    call(client, "POST", "/auth/register", **registration())  # conflict
    call(client, "POST", "/auth/register", **registration(password="short"))
    call(client, "POST", login, identifier="alice", password=PASSWORD)  # unverified
    call(client, "POST", "/auth/resend-verification", email="alice@example.com")
    verification_tokens = [token_from(url) for _, url in outbox.verification]
    call(client, "POST", "/auth/verify-email", token=verification_tokens[0])
    call(client, "POST", "/auth/verify-email", token=verification_tokens[1])
    call(client, "POST", login, identifier="alice", password="wrong")
    call(client, "POST", login, identifier="alice", password=PASSWORD)
    call(second, "POST", login, identifier="alice", password=PASSWORD)
    session_tokens = [session_token(client), session_token(second)]
    call(client, "GET", "/auth/me")
    call(client, "GET", "/auth/sessions")
    call(client, "DELETE", f"/auth/sessions/{uuid.uuid4()}")
    call(client, "POST", "/auth/sessions/revoke-others")
    call(second, "GET", "/auth/me")  # 401
    call(client, "POST", "/auth/forgot-password", email="alice@example.com")
    reset_token = token_from(outbox.password_reset[0][1])
    call(client, "POST", reset, token="wrong", new_password=NEW_PASSWORD)
    call(client, "POST", reset, token=reset_token, new_password="short")
    call(client, "POST", reset, token=reset_token, new_password=NEW_PASSWORD)
    call(client, "POST", login, identifier="alice", password=NEW_PASSWORD)
    session_tokens.append(session_token(client))
    call(client, "POST", "/auth/logout")

    seen = {response.status_code for response in responses}
    assert seen >= {200, 201, 202, 204, 400, 401, 403, 404, 409, 422}

    raw_tokens = [*verification_tokens, *session_tokens, reset_token]
    stored_hashes = [
        value
        for model in (UserSession, EmailVerificationToken, PasswordResetToken)
        for value in session.scalars(select(model.token_hash))
    ]
    assert len(stored_hashes) == 5
    password_hash = session.scalars(select(User.password_hash)).one()
    secrets = [PASSWORD, NEW_PASSWORD, password_hash, *raw_tokens, *stored_hashes]

    for response in responses:
        if response.content:
            assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS), response.url
        for secret in secrets:
            assert secret not in response.text, response.url
        # A session token travels in exactly one place: the Set-Cookie header.
        other_headers = "\n".join(
            f"{name}: {value}"
            for name, value in response.headers.items()
            if name != "set-cookie"
        )
        for secret in secrets:
            assert secret not in other_headers, response.url


def test_unexpected_error_reveals_nothing_about_itself(
    make_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")

    monkeypatch.setattr(auth_service, "register_user", fail)
    client = make_client(raise_server_exceptions=False)

    response = client.post("/auth/register", json=registration())

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "Traceback" not in response.text


def test_every_error_has_the_same_shape(alice_client: TestClient) -> None:
    wrong_login = {"identifier": "alice", "password": "wrong"}
    errors = [
        alice_client.post("/auth/register", json={}),  # 422
        alice_client.post("/auth/register", json=registration()),  # 409
        alice_client.post("/auth/login", json=wrong_login),  # 401
        alice_client.post("/auth/verify-email", json={"token": "wrong"}),  # 400
        alice_client.delete(f"/auth/sessions/{uuid.uuid4()}"),  # 404
        alice_client.post("/auth/logout", headers={"Origin": FOREIGN_ORIGIN}),  # 403
        alice_client.get("/no-such-route"),  # 404
    ]

    statuses = [response.status_code for response in errors]
    assert statuses == [422, 409, 401, 400, 404, 403, 404]
    for response in errors:
        assert set(response.json()) == {"detail"}


def test_database_errors_do_not_carry_statement_parameters(session: Session) -> None:
    # An unhandled database error ends up in the server log. Its message must
    # not include the values of the failed statement.
    token_hash = hash_token(generate_token())
    now = datetime.now(timezone.utc)
    session.add(
        UserSession(
            user_id=uuid.uuid4(),  # no such user
            token_hash=token_hash,
            expires_at=now + timedelta(days=1),
            last_used_at=now,
        )
    )

    with pytest.raises(IntegrityError) as raised:
        session.flush()

    assert token_hash not in str(raised.value)


# --- what logs may contain -----------------------------------------------


def test_raw_tokens_and_passwords_do_not_appear_in_logs(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The real production email sender, not the test outbox.
    del app.dependency_overrides[get_email_sender]
    assert isinstance(get_email_sender(), DisabledEmailSender)
    generated = []

    def recording_generate_token() -> str:
        generated.append(generate_token())
        return generated[-1]

    monkeypatch.setattr(auth_service, "generate_token", recording_generate_token)
    caplog.set_level(logging.DEBUG)

    client.post("/auth/register", json=registration())
    verification_token = generated[0]
    client.post("/auth/resend-verification", json={"email": "alice@example.com"})
    client.post("/auth/verify-email", json={"token": verification_token})  # superseded
    client.post("/auth/verify-email", json={"token": generated[1]})
    log_in(client, "alice", "wrong password")
    log_in(client)
    client.get("/auth/me")
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    client.post(
        "/auth/reset-password",
        json={"token": generated[3], "new_password": NEW_PASSWORD},
    )
    log_in(client, "alice", NEW_PASSWORD)
    client.post("/auth/logout")

    # verification, verification, session, reset, session
    assert len(generated) == 5
    assert session_token(client) is None
    # Something was logged, so this is not passing on an empty log.
    assert "No email provider is configured" in caplog.text

    password_hash = session.scalars(select(User.password_hash)).one()
    for secret in (PASSWORD, NEW_PASSWORD, "wrong password", password_hash):
        assert secret not in caplog.text
    for raw_token in generated:
        assert raw_token not in caplog.text
        assert hash_token(raw_token) not in caplog.text
    assert "alice@example.com" not in caplog.text


def test_email_links_are_logged_only_in_development(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert isinstance(get_email_sender(), DisabledEmailSender)

    monkeypatch.setattr(settings, "environment", "development")

    sender = get_email_sender()
    assert isinstance(sender, ConsoleEmailSender)
    sender.send_email_verification(to="a@example.com", url="https://x/?token=T1")
    sender.send_password_reset(to="a@example.com", url="https://x/?token=T2")
    assert "token=T1" in caplog.text
    assert "token=T2" in caplog.text


def test_production_email_sender_logs_neither_link_nor_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sender = DisabledEmailSender()

    sender.send_email_verification(to="a@example.com", url="https://x/?token=T1")
    sender.send_password_reset(to="a@example.com", url="https://x/?token=T2")

    assert len(caplog.records) == 2
    assert "token" not in caplog.text
    assert "a@example.com" not in caplog.text
