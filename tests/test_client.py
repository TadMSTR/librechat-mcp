"""Client tests — response decoding, JWT lifetime, login rate limiting.

The `content=b"..."` form in these tests is deliberate and load-bearing: it is the
only httpx constructor that produces a response with **no content-type header at
all**, which is what LibreChat actually returns for a uaParser rejection. `text=...`
would set `text/plain` and `json=...` sets `application/json`, so either would test a
different — and easier — case than the real one.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

import librechat_mcp.client as client_mod
from librechat_mcp.client import (
    LibreChatClient,
    LibreChatConfigError,
    LibreChatError,
    _decode_json,
    _jwt_expiry,
    _raise_for_status,
    _sse_error_message,
)

from .conftest import BASE_URL

# The exact bytes captured from the live server on 2026-09-05.
SSE_ERROR_BODY = b'event: error\ndata: {"message":"Illegal request"}\n\n'


def _jwt(**claims) -> str:
    """Build an unsigned JWT-shaped token. The signature is never checked here."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


async def _wait_for_lock_waiter(lock: asyncio.Lock, *, timeout: float = 2.0) -> None:
    """Block until a second coroutine is actually queued on `lock`.

    Reaches for `_waiters` because asyncio.Lock exposes no public equivalent, and a
    fixed `sleep(0)` is not a substitute: the contention tests here are only
    meaningful if the second caller is provably inside the lock, and a test that
    merely hopes so passes just as green when it is not.

    Raises rather than returning quietly on timeout — a silent give-up would turn
    this back into the hopeful sleep it exists to replace.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not lock._waiters:  # type: ignore[attr-defined]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("no coroutine ever queued on the login lock")
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# _raise_for_status
# ---------------------------------------------------------------------------


def test_raise_for_status_success():
    _raise_for_status(httpx.Response(200, json={"ok": True}))  # should not raise


def test_raise_for_status_json_message():
    with pytest.raises(LibreChatError) as exc_info:
        _raise_for_status(httpx.Response(400, json={"message": "bad request"}))
    assert "400" in str(exc_info.value)
    assert "bad request" in str(exc_info.value)


def test_raise_for_status_500():
    with pytest.raises(LibreChatError) as exc_info:
        _raise_for_status(httpx.Response(500, json={"error": "internal error"}))
    assert exc_info.value.status_code == 500


def test_raise_for_status_unwraps_an_sse_error_on_a_real_error_status():
    """An SSE-framed body must read the same whether the status is 200 or 4xx."""
    with pytest.raises(LibreChatError) as exc_info:
        _raise_for_status(httpx.Response(403, content=SSE_ERROR_BODY))
    assert "Illegal request" in str(exc_info.value)


def test_raise_for_status_falls_back_to_reason_phrase_on_an_empty_body():
    with pytest.raises(LibreChatError) as exc_info:
        _raise_for_status(httpx.Response(502))
    assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# _sse_error_message
# ---------------------------------------------------------------------------


def test_sse_error_message_returns_none_for_ordinary_bodies():
    assert _sse_error_message('{"data": []}') is None


def test_sse_error_message_extracts_the_live_shape():
    assert _sse_error_message(SSE_ERROR_BODY.decode()) == "Illegal request"


def test_sse_error_message_handles_an_error_key():
    assert _sse_error_message('event: error\ndata: {"error":"nope"}\n\n') == "nope"


def test_sse_error_message_handles_a_non_json_data_line():
    assert _sse_error_message("event: error\ndata: plain text\n\n") == "plain text"


def test_sse_error_message_handles_a_non_dict_data_line():
    assert _sse_error_message('event: error\ndata: "scalar"\n\n') == "scalar"


def test_sse_error_message_handles_a_missing_data_line():
    assert "no data line" in _sse_error_message("event: error\n\n")


# ---------------------------------------------------------------------------
# _decode_json — the guard that did not exist
# ---------------------------------------------------------------------------


def test_decode_json_returns_the_parsed_body():
    assert _decode_json(httpx.Response(200, json={"ok": True})) == {"ok": True}


def test_decode_json_accepts_a_charset_parameter():
    """LibreChat sends `application/json; charset=utf-8`, not a bare `application/json`."""
    resp = httpx.Response(
        200, content=b'{"ok":true}', headers={"content-type": "application/json; charset=utf-8"}
    )
    assert _decode_json(resp) == {"ok": True}


def test_decode_json_accepts_the_structured_json_suffix():
    resp = httpx.Response(
        200, content=b'{"ok":true}', headers={"content-type": "application/vnd.api+json"}
    )
    assert _decode_json(resp) == {"ok": True}


def test_decode_json_raises_librechaterror_on_the_200_sse_rejection():
    """THE regression test. HTTP 200, no content-type, SSE error body.

    Before this guard: `_raise_for_status` passed on 200, `resp.json()` raised
    `json.JSONDecodeError`, and that class was in no `except` clause anywhere on the
    path — so it escaped as a raw traceback. Six weeks, every tool, nothing logged.
    """
    resp = httpx.Response(200, content=SSE_ERROR_BODY)
    assert resp.headers.get("content-type") is None  # the premise, asserted not assumed

    with pytest.raises(LibreChatError) as exc_info:
        _decode_json(resp)
    # EXACT, not `"Illegal request" in ...`. The content-type branch echoes the raw
    # body into its message, so a substring check passes just as well with the SSE
    # parsing removed entirely — it cannot tell "parsed the error" from "dumped the
    # response". Caught by mutation: dropping the SSE branch left that check green.
    assert str(exc_info.value) == "LibreChat error 200: Illegal request"
    assert exc_info.value.status_code == 200


def test_decode_json_treats_a_missing_content_type_as_failure():
    """A MISSING header must fail, not pass.

    The natural spelling of this check — `content_type.startswith("application/json")`
    against a `.get(...)` default of `None` — throws; against a default of `""` it
    returns False, which is right by accident. This asserts the behaviour directly so
    a later refactor to a negative test ("is it text/html?") cannot reintroduce the
    hole, since an absent header is not text/html either.
    """
    resp = httpx.Response(200, content=b"<html>not json</html>")
    assert resp.headers.get("content-type") is None

    with pytest.raises(LibreChatError) as exc_info:
        _decode_json(resp)
    assert "<absent>" in str(exc_info.value)


def test_decode_json_rejects_a_non_json_content_type():
    resp = httpx.Response(200, text="totally fine, just not json")
    with pytest.raises(LibreChatError) as exc_info:
        _decode_json(resp)
    assert "text/plain" in str(exc_info.value)


def test_decode_json_raises_librechaterror_on_malformed_json():
    """Even when the server claims JSON, a JSONDecodeError must not escape."""
    resp = httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"})
    with pytest.raises(LibreChatError) as exc_info:
        _decode_json(resp)
    assert "malformed JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _jwt_expiry
# ---------------------------------------------------------------------------


def test_jwt_expiry_reads_the_exp_claim():
    exp = int(datetime.now(tz=UTC).timestamp()) + 900
    assert _jwt_expiry(_jwt(exp=exp)) == datetime.fromtimestamp(exp, tz=UTC)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("not-a-jwt", id="no-dots"),
        pytest.param("header..signature", id="empty-payload"),
        pytest.param("header.%%%.signature", id="undecodable-base64"),
        pytest.param(_jwt(sub="nobody"), id="no-exp-claim"),
        pytest.param(_jwt(exp="soon"), id="exp-not-a-number"),
        pytest.param(_jwt(exp=True), id="exp-is-a-bool"),
        pytest.param(_jwt(exp=10**20), id="exp-out-of-range"),
    ],
)
def test_jwt_expiry_returns_none_for_anything_undecodable(token):
    """Every one of these must fall back to the measured lifetime, not crash and not
    be treated as an immortal token."""
    assert _jwt_expiry(token) is None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_missing_url_raises_config_error(monkeypatch):
    monkeypatch.delenv("LIBRECHAT_URL", raising=False)
    with pytest.raises(LibreChatConfigError, match="LIBRECHAT_URL"):
        LibreChatClient()


def test_missing_credentials_raise_config_error(monkeypatch):
    monkeypatch.setenv("LIBRECHAT_URL", BASE_URL)
    monkeypatch.delenv("LIBRECHAT_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("LIBRECHAT_ADMIN_PASSWORD", raising=False)
    with pytest.raises(LibreChatConfigError, match="LIBRECHAT_ADMIN_EMAIL"):
        LibreChatClient()


def test_get_client_returns_the_same_instance(librechat_env):
    client_mod._client = None
    try:
        assert client_mod.get_client() is client_mod.get_client()
    finally:
        client_mod._client = None


# ---------------------------------------------------------------------------
# JWT lifetime and refresh
# ---------------------------------------------------------------------------


async def test_login_sets_expiry_from_the_token_itself(librechat_env):
    """The whole point of the v0.2.0 change: the server tells us, we do not guess."""
    exp = int(datetime.now(tz=UTC).timestamp()) + 900
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": _jwt(exp=exp)})
        )
        client = LibreChatClient()
        await client._login()
        assert client._jwt_expires_at == datetime.fromtimestamp(exp, tz=UTC)
        await client.close()


async def test_login_falls_back_to_the_measured_lifetime_for_an_opaque_token(librechat_env):
    """An undecodable token must not be treated as never expiring."""
    before = datetime.now(tz=UTC)
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": "opaque"})
        )
        client = LibreChatClient()
        await client._login()
        assert client._jwt_expires_at is not None
        skew = client._jwt_expires_at - (before + client_mod._FALLBACK_TOKEN_LIFETIME)
        assert abs(skew) < timedelta(seconds=5)
        await client.close()


async def test_a_token_inside_the_refresh_margin_is_not_fresh(librechat_env):
    """The margin is the point — a token expiring in 30s must not be handed out to a
    request that may take longer than that."""
    with respx.mock(base_url=BASE_URL):
        client = LibreChatClient()
        client._jwt = "about-to-expire"

        client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(seconds=30)
        assert not client._jwt_is_fresh()

        client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(seconds=600)
        assert client._jwt_is_fresh()

        client._jwt = None
        assert not client._jwt_is_fresh()
        await client.close()


async def test_concurrent_calls_on_an_expired_token_log_in_exactly_once(librechat_env):
    """Ten concurrent tool calls on an expired token must produce ONE login.

    Note what this does NOT prove: respx resolves the login without yielding, so the
    first caller finishes before the second starts and every later caller returns
    from the fast path at the top of `_get_jwt`, never entering the lock. The
    outcome is right, but the branch that makes it right under real concurrency is
    not exercised here. `test_a_second_caller_blocked_on_the_lock_*` below is the
    test that actually forces two coroutines inside it.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        login = mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": _jwt(exp=2**31 - 1)})
        )
        agents = mock.get("/api/agents").mock(return_value=httpx.Response(200, json={"data": []}))
        client = LibreChatClient()
        await asyncio.gather(*(client.request("GET", "/api/agents") for _ in range(10)))

        assert login.call_count == 1
        assert agents.call_count == 10
        await client.close()


async def test_a_second_caller_blocked_on_the_lock_reuses_the_new_token(librechat_env):
    """The double-check inside `_get_jwt`'s lock, actually executed.

    A login that resolves instantly cannot produce contention, so this gates the
    login response on an event: caller one is held mid-login while caller two piles
    up on the lock, and only then is it released. Caller two therefore takes the
    re-check branch rather than the fast path — which is the branch that turns a
    stampede into one login, and which no instantaneous mock can reach.
    """
    reached_login = asyncio.Event()
    release_login = asyncio.Event()
    logins = 0

    async def gated_login(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        logins += 1
        reached_login.set()
        await release_login.wait()
        return httpx.Response(200, json={"token": _jwt(exp=2**31 - 1)})

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/auth/login").mock(side_effect=gated_login)
        mock.get("/api/agents").mock(return_value=httpx.Response(200, json={"data": []}))

        client = LibreChatClient()
        first = asyncio.create_task(client.request("GET", "/api/agents"))
        await reached_login.wait()  # caller one is inside the lock, mid-login

        second = asyncio.create_task(client.request("GET", "/api/agents"))
        # Let caller two run as far as the contended lock and block there. The lock
        # is already held by caller one, so this is deterministic, not a sleep-race.
        await asyncio.sleep(0)
        assert client._login_lock.locked()

        release_login.set()
        assert await first == {"data": []}
        assert await second == {"data": []}
        assert logins == 1
        await client.close()


async def test_a_second_caller_blocked_after_a_401_does_not_log_in_again(librechat_env):
    """The double-check in `_relogin_after_401`, against the token comparison.

    Reaching this branch takes real care, because `_relogin_after_401` nulls `_jwt`
    before it logs in. Any caller that has not yet read the token therefore fails
    `_jwt_is_fresh()` and queues on `_get_jwt`'s lock instead — a different branch
    that produces the same "one login" outcome. The obvious version of this test
    goes there and passes without ever running the line it names.

    So both callers are held mid-GET, past `_get_jwt` and each holding the stale
    token, before either can 401. Only then do they both arrive at
    `_relogin_after_401` with the same stale token in hand, which is the situation
    the comparison exists for.
    """
    fresh = _jwt(exp=2**31 - 1)
    release_first_gets = asyncio.Event()
    both_in_flight = asyncio.Event()
    reached_login = asyncio.Event()
    release_login = asyncio.Event()
    logins = 0
    gets_in_flight = 0

    async def gated_login(request: httpx.Request) -> httpx.Response:
        nonlocal logins
        logins += 1
        reached_login.set()
        await release_login.wait()
        return httpx.Response(200, json={"token": fresh})

    async def gated_get(request: httpx.Request) -> httpx.Response:
        nonlocal gets_in_flight
        if request.headers["authorization"] == f"Bearer {fresh}":
            return httpx.Response(200, json={"data": []})  # the post-refresh retry
        gets_in_flight += 1
        if gets_in_flight == 2:
            both_in_flight.set()
        await release_first_gets.wait()
        return httpx.Response(401, json={"message": "Unauthorized"})

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/auth/login").mock(side_effect=gated_login)
        mock.get("/api/agents").mock(side_effect=gated_get)

        client = LibreChatClient()
        client._jwt = "stale-token"
        client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(hours=1)

        first = asyncio.create_task(client.request("GET", "/api/agents"))
        second = asyncio.create_task(client.request("GET", "/api/agents"))

        # Both past _get_jwt, both holding "stale-token", neither has 401ed yet.
        await both_in_flight.wait()
        release_first_gets.set()

        # One of them takes the lock and logs in; the other queues behind it in
        # _relogin_after_401 — provably, not hopefully.
        await reached_login.wait()
        await _wait_for_lock_waiter(client._login_lock)

        release_login.set()
        assert await first == {"data": []}
        assert await second == {"data": []}
        assert logins == 1
        await client.close()


async def test_a_401_storm_does_not_become_a_login_storm(librechat_env):
    """Every in-flight request holding the same stale token 401s at once. Only the
    first should re-login; the rest must notice the token already changed."""
    fresh = _jwt(exp=2**31 - 1)
    with respx.mock(base_url=BASE_URL) as mock:
        login = mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": fresh})
        )

        def respond(request: httpx.Request) -> httpx.Response:
            if request.headers["authorization"] == f"Bearer {fresh}":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(401, json={"message": "Unauthorized"})

        mock.get("/api/agents").mock(side_effect=respond)

        client = LibreChatClient()
        client._jwt = "stale-token"
        client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(hours=1)

        results = await asyncio.gather(*(client.request("GET", "/api/agents") for _ in range(5)))
        assert all(r == {"data": []} for r in results)
        assert login.call_count == 1
        await client.close()


async def test_request_retries_once_on_401(librechat_env):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": _jwt(exp=2**31 - 1)})
        )
        mock.get("/api/agents").mock(
            side_effect=[
                httpx.Response(401, json={"message": "Unauthorized"}),
                httpx.Response(200, json=[]),
            ]
        )
        client = LibreChatClient()
        client._jwt = "stale-token"
        client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(hours=1)

        assert await client.request("GET", "/api/agents") == []
        await client.close()


async def test_request_returns_none_for_204_and_empty_bodies(librechat_env):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.delete("/api/agents/a1").mock(return_value=httpx.Response(204))
        client = LibreChatClient()
        client._jwt = "test-token"
        client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(hours=1)

        assert await client.request("DELETE", "/api/agents/a1") is None
        await client.close()


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------


async def test_login_retries_on_429_then_succeeds(librechat_env, monkeypatch):
    """Ride out a short burst rather than failing the first tool call after expiry."""
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/auth/login").mock(
            side_effect=[
                httpx.Response(429, json={"message": "Too many login attempts"}),
                httpx.Response(200, json={"token": _jwt(exp=2**31 - 1)}),
            ]
        )
        client = LibreChatClient()
        await client._login()
        assert client._jwt is not None
        assert slept == [client_mod._LOGIN_BACKOFF_SECONDS[0]]
        await client.close()


async def test_login_surfaces_a_persistent_429_rather_than_waiting_it_out(
    librechat_env, monkeypatch
):
    """A five-minute sleep inside a tool call presents to the caller as a hang. The
    429 must arrive as itself, naming the limit, after a bounded number of tries."""
    slept: list[float] = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)

    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/auth/login").mock(
            return_value=httpx.Response(
                429, json={"message": "Too many login attempts, please try again after 5 minutes"}
            )
        )
        client = LibreChatClient()
        with pytest.raises(LibreChatError) as exc_info:
            await client._login()

        assert exc_info.value.status_code == 429
        assert "Too many login attempts" in str(exc_info.value)
        assert route.call_count == client_mod._LOGIN_MAX_ATTEMPTS
        assert slept == list(client_mod._LOGIN_BACKOFF_SECONDS)
        await client.close()
