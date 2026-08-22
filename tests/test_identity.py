"""Tests for per-user identity: token minting, user lookup and client resolution."""

import time

import httpx
import jwt as pyjwt
import pytest
import respx

from mcp_superset import context
from mcp_superset.auth import CookieAuthManager
from mcp_superset.client import SupersetClient
from mcp_superset.identity import (
    ImpersonationJwtAuth,
    SupersetIdentityError,
    UserClientRegistry,
    UserDirectory,
)

BASE = "https://superset.example.com"
SECRET = "test-secret-long-enough-for-hs256-hmac"


def _service_client() -> SupersetClient:
    return SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)


def _decode(token: str) -> dict:
    return pyjwt.decode(token, SECRET, algorithms=["HS256"])


async def test_minted_token_matches_superset_token_layout():
    auth = ImpersonationJwtAuth(base_url=BASE, user_id=42, secret=SECRET)
    headers: dict[str, str] = {}
    await auth.apply_auth(httpx.AsyncClient(), headers)

    claims = _decode(headers["Authorization"].removeprefix("Bearer "))
    assert claims["sub"] == "42"  # Superset stores the user id as a string
    assert claims["type"] == "access"
    assert {"iat", "nbf", "exp", "jti", "csrf", "fresh"} <= set(claims)
    assert claims["exp"] > time.time()


async def test_token_is_cached_until_close_to_expiry():
    auth = ImpersonationJwtAuth(base_url=BASE, user_id=1, secret=SECRET)
    first = {}
    second = {}
    await auth.apply_auth(httpx.AsyncClient(), first)
    await auth.apply_auth(httpx.AsyncClient(), second)
    assert first["Authorization"] == second["Authorization"]

    auth.invalidate()
    third = {}
    await auth.apply_auth(httpx.AsyncClient(), third)
    assert third["Authorization"] != first["Authorization"]


@respx.mock
async def test_directory_finds_user_by_email():
    respx.get(f"{BASE}/api/v1/security/users/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "result": [{"id": 7, "username": "google_123", "email": "Someone@Example.com", "active": True}],
            },
        )
    )
    directory = UserDirectory(service_client=_service_client())
    user = await directory.resolve("someone@example.com")
    assert (user.id, user.username) == (7, "google_123")


@respx.mock
async def test_directory_rejects_unknown_email_with_actionable_message():
    respx.get(f"{BASE}/api/v1/security/users/").mock(return_value=httpx.Response(200, json={"count": 0, "result": []}))
    directory = UserDirectory(service_client=_service_client())
    with pytest.raises(SupersetIdentityError, match="Sign in to Superset once"):
        await directory.resolve("nobody@example.com")


@respx.mock
async def test_directory_rejects_deactivated_user():
    respx.get(f"{BASE}/api/v1/security/users/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "result": [{"id": 9, "username": "old", "email": "old@example.com", "active": False}],
            },
        )
    )
    directory = UserDirectory(service_client=_service_client())
    with pytest.raises(SupersetIdentityError, match="deactivated"):
        await directory.resolve("old@example.com")


@respx.mock
async def test_registry_returns_one_client_per_user_and_mints_their_token():
    respx.get(f"{BASE}/api/v1/security/users/").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 1,
                "result": [{"id": 11, "username": "u", "email": "u@example.com", "active": True}],
            },
        )
    )
    me = respx.get(f"{BASE}/api/v1/me/").mock(return_value=httpx.Response(200, json={"result": {"id": 11}}))

    registry = UserClientRegistry(
        base_url=BASE,
        jwt_secret=SECRET,
        directory=UserDirectory(service_client=_service_client()),
    )
    client, user = await registry.client_for("u@example.com")
    again, _ = await registry.client_for("U@Example.com")
    assert client is again  # same user -> same cached client
    assert user.id == 11

    await client.get("/api/v1/me/")
    sent = me.calls.last.request.headers["authorization"].removeprefix("Bearer ")
    assert _decode(sent)["sub"] == "11"

    await registry.aclose()


def test_caller_email_requires_authentication():
    context.configure(service_client=_service_client())
    with pytest.raises(SupersetIdentityError, match="not authenticated"):
        context.caller_email()


class _FakeToken:
    def __init__(self, email: str):
        self.claims = {"email": email}


def test_caller_email_enforces_allowed_domains(monkeypatch):
    context.configure(service_client=_service_client(), allowed_domains={"example.com"})
    monkeypatch.setattr(context, "get_access_token", lambda: _FakeToken("me@example.com"))
    assert context.caller_email() == "me@example.com"

    monkeypatch.setattr(context, "get_access_token", lambda: _FakeToken("me@gmail.com"))
    with pytest.raises(SupersetIdentityError, match="not allowed"):
        context.caller_email()

    context.configure(service_client=_service_client())


async def test_service_mode_ignores_identity():
    service = _service_client()
    context.configure(service_client=service)
    assert not context.per_user_mode()
    assert await context.resolve_client() is service
