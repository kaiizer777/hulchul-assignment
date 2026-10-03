"""
Tests for cookie-session authentication, the session guard, and the CORS allowlist.

Session tests run against the in-process mock Upstash server from conftest, so no
real Upstash instance and no real database is involved.
"""

import dataclasses
import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

import backend.auth as auth_module
from backend import auth
from backend.config import settings
from backend.main import app, get_allowed_origins
from backend.redis_client import UpstashRedisClient
from conftest import storage

OPERATOR_PASSWORD = "correct horse battery staple"

# Cost parameters are deliberately tiny: these hashes exist to exercise argon2id
# verification, not to benchmark it.
_fast_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
PASSWORD_HASH = _fast_hasher.hash(OPERATOR_PASSWORD)

SESSION_KEY_PREFIX = "hulchul:auth:session:"

# Interactive docs and the OpenAPI schema. None of these sit behind the session
# guard, so each one is an unauthenticated dump of every route.
DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}


def _forget_auth_keys() -> None:
    for key in [key for key in storage if key.startswith("hulchul:auth:")]:
        del storage[key]


@pytest.fixture(autouse=True)
def isolated_session_store():
    """The mock Upstash store is module-level and shared; give every test a clean slate."""
    _forget_auth_keys()
    yield
    _forget_auth_keys()


@pytest.fixture
def client() -> TestClient:
    """In-process client. Not used as a context manager, so the DB pool never starts."""
    return TestClient(app)


@pytest.fixture
def https_client() -> TestClient:
    """Same app, but reached over HTTPS so the `__Host-` cookie path is exercised."""
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def configured_password(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the auth module at a known-good argon2id hash."""
    monkeypatch.setattr(
        auth_module,
        "settings",
        dataclasses.replace(settings, AUTH_PASSWORD_HASH=PASSWORD_HASH),
    )
    return PASSWORD_HASH


def _unset_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_module,
        "settings",
        dataclasses.replace(settings, AUTH_PASSWORD_HASH=""),
    )


def _session_keys() -> list[str]:
    return [key for key in storage if key.startswith(SESSION_KEY_PREFIX)]


def _store_session(ttl_seconds: int) -> tuple[str, str]:
    """Write a session record straight into the mock store. Returns (token, digest)."""
    token = auth.generate_session_token()
    digest = auth.hash_session_token(token)
    now = int(time.time())
    storage[f"{SESSION_KEY_PREFIX}{digest}"] = json.dumps(
        {"sub": "operator", "iat": now, "exp": now + ttl_seconds}
    )
    return token, digest


# ---------------------------------------------------------------------------
# Token format
# ---------------------------------------------------------------------------

def test_session_token_is_32_random_bytes_base64url_unpadded() -> None:
    """Tokens must be 32 random bytes, base64url encoded, without padding."""
    tokens = {auth.generate_session_token() for _ in range(20)}
    assert len(tokens) == 20, "session tokens must be unique"

    for token in tokens:
        assert "=" not in token, "base64url session tokens must be unpadded"
        assert len(token) == 43, "32 bytes base64url encode to 43 characters"
        assert len(auth.hash_session_token(token)) == 64
        assert auth.hash_session_token(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def test_login_with_correct_password_sets_hardened_cookie(
    https_client: TestClient, configured_password: str
) -> None:
    """A successful login over HTTPS must set a __Host- prefixed, fully hardened cookie."""
    response = https_client.post("/auth/login", json={"password": OPERATOR_PASSWORD})

    assert response.status_code == 200
    assert response.json() == {"sub": "operator", "exp": response.json()["exp"]}
    assert isinstance(response.json()["exp"], int)

    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith("__Host-hulchul_session=")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")
    assert "Path=/" in set_cookie
    assert f"Max-Age={settings.AUTH_SESSION_TTL_SECONDS}" in set_cookie
    assert "Domain" not in set_cookie, "__Host- cookies must not carry a Domain attribute"


def test_login_over_plain_http_uses_non_prefixed_cookie(client: TestClient, configured_password: str) -> None:
    """Browsers reject __Host- on plain HTTP, so dev must get an unprefixed cookie."""
    response = client.post("/auth/login", json={"password": OPERATOR_PASSWORD})

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith("hulchul_session=")
    assert "HttpOnly" in set_cookie
    assert "Secure" not in set_cookie, "Secure must only be set for HTTPS requests"
    assert "SameSite=lax" in set_cookie.replace("samesite", "SameSite")


def test_login_sets_cookie_for_https_behind_forwarded_proto(
    client: TestClient, configured_password: str
) -> None:
    """TLS terminates before the app, so the proxy hop hint must be honoured."""
    response = client.post(
        "/auth/login",
        json={"password": OPERATOR_PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith("__Host-hulchul_session=")
    assert "Secure" in set_cookie


def test_login_with_wrong_password_returns_401(client: TestClient, configured_password: str) -> None:
    """A wrong password is a generic 401 with no session and no cookie."""
    response = client.post("/auth/login", json={"password": "wrong password"})

    assert response.status_code == 401
    assert response.json() == {"detail": auth.INVALID_CREDENTIALS_DETAIL}
    assert "set-cookie" not in response.headers
    assert _session_keys() == [], "a rejected login must not persist a session"


@pytest.mark.parametrize(
    "stored_hash",
    [
        "a-bcrypt-hash-pasted-by-mistake",  # another algorithm's format entirely
        "$argon2id$v=19$m=8,t=1,p=1$c2FsdA",  # truncated PHC string
        "argon2id$v=19$m=65536,t=3,p=4$salt$hash",  # missing the leading '$'
    ],
)
def test_login_with_an_unparseable_password_hash_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stored_hash: str
) -> None:
    """
    An unusable stored hash must read as a credential failure, never as a crash.

    argon2 raises InvalidHashError for a hash it cannot parse, and that is a
    ValueError rather than a VerificationError. It used to escape verify_password
    entirely, so a misconfigured hash turned every login attempt into an
    unhandled 500 with a stack trace instead of a generic 401.
    """
    monkeypatch.setattr(
        auth_module,
        "settings",
        dataclasses.replace(settings, AUTH_PASSWORD_HASH=stored_hash),
    )

    for candidate in (OPERATOR_PASSWORD, "wrong password"):
        response = client.post("/auth/login", json={"password": candidate})
        assert response.status_code == 401, f"hash {stored_hash!r} must not authenticate"
        assert response.json() == {"detail": auth.INVALID_CREDENTIALS_DETAIL}
        assert "set-cookie" not in response.headers

    assert _session_keys() == []


def test_login_without_password_hash_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Fail closed: an unset AUTH_PASSWORD_HASH must never let anyone in, not even with
    the correct password, and must never fall back to a default secret.
    """
    _unset_password(monkeypatch)

    for candidate in ("", " ", OPERATOR_PASSWORD, "anything at all"):
        response = client.post("/auth/login", json={"password": candidate})
        assert response.status_code == 503, f"candidate {candidate!r} must be rejected"
        assert response.json() == {"detail": auth.AUTH_UNAVAILABLE_DETAIL}
        assert "set-cookie" not in response.headers, "no cookie may be issued"

    assert _session_keys() == [], "an unconfigured hash must not create sessions"

    verify = client.get("/auth/session")
    assert verify.status_code == 401, "no session may exist after failed logins"


def test_login_stores_only_the_token_digest(client: TestClient, configured_password: str) -> None:
    """The raw token must never be persisted, logged, or used as a Redis key."""
    response = client.post("/auth/login", json={"password": OPERATOR_PASSWORD})
    assert response.status_code == 200

    raw_token = response.cookies["hulchul_session"]
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    session_keys = _session_keys()
    assert session_keys == [f"{SESSION_KEY_PREFIX}{digest}"]

    for key in storage:
        assert raw_token not in key, "the raw session token must never appear in a Redis key"
        assert raw_token not in str(storage[key]), "the raw session token must never be persisted"

    record = json.loads(storage[f"{SESSION_KEY_PREFIX}{digest}"])
    assert record["sub"] == "operator"
    assert isinstance(record["iat"], int)
    assert isinstance(record["exp"], int)
    assert record["exp"] == record["iat"] + settings.AUTH_SESSION_TTL_SECONDS


def test_login_rejects_unknown_body_fields(client: TestClient, configured_password: str) -> None:
    """extra="forbid" keeps mass-assignment payloads out of the login body."""
    response = client.post(
        "/auth/login",
        json={"password": OPERATOR_PASSWORD, "sub": "admin", "exp": 9999999999},
    )
    assert response.status_code == 422
    assert _session_keys() == []


# ---------------------------------------------------------------------------
# Session validation
# ---------------------------------------------------------------------------

def test_session_round_trip_after_login(client: TestClient, configured_password: str) -> None:
    """A freshly issued cookie must validate against Redis."""
    assert client.post("/auth/login", json={"password": OPERATOR_PASSWORD}).status_code == 200

    response = client.get("/auth/session")

    assert response.status_code == 200
    assert response.json()["sub"] == "operator"
    assert client.get("/tools").status_code == 200, "a valid session must unlock protected routes"


def test_protected_route_without_cookie_returns_401(client: TestClient) -> None:
    """Unauthenticated access to every protected route is a 401, never a 403."""
    protected = [
        ("get", "/tools", None),
        ("post", "/tools/check-exists", {"entity_type": "purchase_order", "identifier": "PO-1"}),
        ("post", "/agent/runs/some-run/pause", None),
        ("post", "/agent/runs/some-run/resume", None),
        ("get", "/agent/runs/some-run/state", None),
        ("get", "/agent/runs/some-run/approval", None),
        ("post", "/agent/runs/some-run/approval", {"decision": "approved"}),
        ("get", "/agent/runs/00000000-0000-0000-0000-000000000000", None),
        ("get", "/agent/runs/00000000-0000-0000-0000-000000000000/steps", None),
        ("get", "/agent/runs/00000000-0000-0000-0000-000000000000/verification", None),
        ("get", "/agent/steps/00000000-0000-0000-0000-000000000000", None),
    ]
    for method, path, body in protected:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, f"{method.upper()} {path} must be guarded"
        assert response.json() == {"detail": auth.UNAUTHENTICATED_DETAIL}


@pytest.mark.parametrize(
    "cookie",
    [
        "hulchul_session=",  # empty
        "hulchul_session=not-a-real-token",  # garbage
        "hulchul_session=../../etc/passwd",  # traversal-ish
        "hulchul_session=" + "A" * 4096,  # oversized
    ],
)
def test_tampered_cookie_returns_401(client: TestClient, cookie: str) -> None:
    """Malformed cookie values are simply unauthenticated."""
    response = client.get("/auth/session", headers={"Cookie": cookie})
    assert response.status_code == 401
    assert response.json() == {"detail": auth.UNAUTHENTICATED_DETAIL}


def test_unknown_but_well_formed_token_returns_401(client: TestClient) -> None:
    """A correctly shaped token that was never issued must not authenticate."""
    cookie = f"hulchul_session={auth.generate_session_token()}"

    response = client.get("/auth/session", headers={"Cookie": cookie})

    assert response.status_code == 401


def test_expired_session_returns_401(client: TestClient) -> None:
    """exp is re-checked in code; the Redis TTL is never trusted on its own."""
    token, digest = _store_session(ttl_seconds=-60)
    assert f"{SESSION_KEY_PREFIX}{digest}" in storage, "record must exist so only exp can reject it"

    response = client.get("/auth/session", headers={"Cookie": f"hulchul_session={token}"})

    assert response.status_code == 401


def test_session_record_with_wrong_subject_is_rejected(client: TestClient) -> None:
    """A record naming any other subject must not authenticate the operator."""
    digest = auth.hash_session_token("forged-token")
    storage[f"{SESSION_KEY_PREFIX}{digest}"] = json.dumps(
        {"sub": "someone-else", "exp": int(time.time()) + 3600}
    )

    response = client.get("/auth/session", headers={"Cookie": "hulchul_session=forged-token"})

    assert response.status_code == 401


def test_malformed_session_record_is_rejected(client: TestClient) -> None:
    """A non-JSON or non-conforming record must fail closed instead of raising."""
    digest = auth.hash_session_token("broken-token")
    storage[f"{SESSION_KEY_PREFIX}{digest}"] = "this-is-not-json"

    response = client.get("/auth/session", headers={"Cookie": "hulchul_session=broken-token"})

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

def test_logout_deletes_the_session_and_is_idempotent(
    client: TestClient, configured_password: str
) -> None:
    """Logout revokes server-side state, clears the cookie, and never 401s."""
    assert client.post("/auth/login", json={"password": OPERATOR_PASSWORD}).status_code == 200
    token = client.cookies["hulchul_session"]
    digest = auth.hash_session_token(token)
    assert f"{SESSION_KEY_PREFIX}{digest}" in storage

    first = client.post("/auth/logout")
    assert first.status_code == 204
    assert f"{SESSION_KEY_PREFIX}{digest}" not in storage, "logout must delete the Redis key"
    assert "Max-Age=0" in first.headers["set-cookie"]

    second = client.post("/auth/logout")
    assert second.status_code == 204, "logout must be idempotent"


def test_logout_without_cookie_returns_204(client: TestClient) -> None:
    """No cookie is not an error."""
    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert response.content == b""


def test_logout_with_unknown_token_returns_204(client: TestClient) -> None:
    """An unknown or already-expired token must not turn logout into a 401."""
    response = client.post(
        "/auth/logout",
        headers={"Cookie": f"hulchul_session={auth.generate_session_token()}"},
    )
    assert response.status_code == 204


def test_logout_with_garbage_cookie_returns_204(client: TestClient) -> None:
    """Garbage cookie values are cleared, not rejected."""
    response = client.post("/auth/logout", headers={"Cookie": "hulchul_session=%%%not-a-token%%%"})

    assert response.status_code == 204


def test_session_is_invalid_after_logout(client: TestClient, configured_password: str) -> None:
    """A revoked token must not work again, even if the client kept the cookie."""
    assert client.post("/auth/login", json={"password": OPERATOR_PASSWORD}).status_code == 200
    token = client.cookies["hulchul_session"]

    assert client.post("/auth/logout").status_code == 204

    replay = client.get("/auth/session", headers={"Cookie": f"hulchul_session={token}"})
    assert replay.status_code == 401


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------

def _throttled_redis() -> MagicMock:
    """A Redis double with a working INCR, which the in-process mock server lacks."""
    mock = MagicMock(spec=UpstashRedisClient)
    mock.is_configured = True
    failures: dict[str, int] = {}

    async def _count(ip: str) -> int:
        return failures.get(ip, 0)

    async def _record(ip: str, window_seconds: int) -> int:
        failures[ip] = failures.get(ip, 0) + 1
        return failures[ip]

    async def _clear(ip: str) -> None:
        failures.pop(ip, None)

    mock.get_login_failure_count = AsyncMock(side_effect=_count)
    mock.record_login_failure = AsyncMock(side_effect=_record)
    mock.clear_login_failures = AsyncMock(side_effect=_clear)
    return mock


def test_eleventh_failed_attempt_in_a_window_is_rate_limited(
    client: TestClient, configured_password: str
) -> None:
    """After 10 failures the 11th attempt is refused with Retry-After, before hashing."""
    redis = _throttled_redis()
    hasher = MagicMock(spec=PasswordHasher)
    hasher.verify.return_value = False

    with (
        patch.object(auth_module, "get_redis_client", return_value=redis),
        patch.object(auth_module, "_password_hasher", hasher),
    ):
        for attempt in range(1, 11):
            response = client.post("/auth/login", json={"password": "wrong"})
            assert response.status_code == 401, f"attempt {attempt} should be a plain 401"

        blocked = client.post("/auth/login", json={"password": "wrong"})

        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "900"
        # Atomic INCR-first gate: the blocked 11th attempt also consumes a slot
        # (gate INCR is the failure record), so 11 INCRs for 11 attempts vs the
        # old GET-gate's 10. Hasher stays at 10: the block happens pre-hash.
        assert redis.record_login_failure.call_count == 11
        # The limit is applied before hashing, so a blocked attempt costs no argon2 work.
        assert hasher.verify.call_count == 10


def test_successful_login_resets_the_failure_counter(
    client: TestClient, configured_password: str
) -> None:
    """A correct password clears the bucket so a typo does not lock the operator out."""
    redis = _throttled_redis()

    with patch.object(auth_module, "get_redis_client", return_value=redis):
        client.post("/auth/login", json={"password": "wrong"})
        client.post("/auth/login", json={"password": "wrong"})
        redis.clear_login_failures.assert_not_called()

        assert client.post("/auth/login", json={"password": OPERATOR_PASSWORD}).status_code == 200

        redis.clear_login_failures.assert_called_once()


def test_rate_limiter_buckets_by_forwarded_for(client: TestClient, configured_password: str) -> None:
    """X-Forwarded-For only selects the bucket; it must not be trusted for authorization."""
    redis = _throttled_redis()

    with patch.object(auth_module, "get_redis_client", return_value=redis):
        for _ in range(10):
            response = client.post(
                "/auth/login",
                json={"password": "wrong"},
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
            assert response.status_code == 401

        blocked = client.post(
            "/auth/login",
            json={"password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        assert blocked.status_code == 429

        # A different client is unaffected by the exhausted bucket.
        other = client.post(
            "/auth/login",
            json={"password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.4"},
        )
        assert other.status_code == 401


def test_concurrent_login_burst_is_bounded_by_atomic_incr(
    configured_password: str,
) -> None:
    """Regression: N concurrent same-bucket attempts must not each burn a hash.

    Sequential tests cannot catch the old GET-count -> verify -> INCR race: N
    concurrent requests all read the same stale count and each burns a full
    argon2 verify. With the INCR-first gate the INCR is server-atomic, so only
    the remaining budget slots reach the hasher and the excess 429s pre-hash.
    """
    import asyncio

    burst_size = 20
    failures: dict[str, int] = {}
    redis = MagicMock(spec=UpstashRedisClient)
    redis.is_configured = True

    async def _count(ip: str) -> int:
        await asyncio.sleep(0)
        return failures.get(ip, 0)

    async def _record(ip: str, window_seconds: int) -> int:
        # Atomic like real Redis INCR: mutate before yielding, so concurrent
        # callers still observe distinct sequential counts.
        failures[ip] = failures.get(ip, 0) + 1
        count = failures[ip]
        await asyncio.sleep(0)
        return count

    async def _clear(ip: str) -> None:
        failures.pop(ip, None)

    redis.get_login_failure_count = AsyncMock(side_effect=_count)
    redis.record_login_failure = AsyncMock(side_effect=_record)
    redis.clear_login_failures = AsyncMock(side_effect=_clear)

    hasher = MagicMock(spec=PasswordHasher)
    hasher.verify.return_value = False

    async def _burst() -> list:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as async_client:
            return await asyncio.gather(
                *[
                    async_client.post(
                        "/auth/login",
                        json={"password": "wrong"},
                        headers={"X-Forwarded-For": "203.0.113.99"},
                    )
                    for _ in range(burst_size)
                ]
            )

    with (
        patch.object(auth_module, "get_redis_client", return_value=redis),
        patch.object(auth_module, "_password_hasher", hasher),
    ):
        responses = _run(_burst())

    statuses = [response.status_code for response in responses]
    assert len(statuses) == burst_size
    assert set(statuses) <= {401, 429}
    # Only the remaining budget slots may burn a hash; the excess 429s pre-hash.
    assert hasher.verify.call_count <= auth.LOGIN_MAX_FAILURES
    assert statuses.count(429) >= burst_size - auth.LOGIN_MAX_FAILURES
    # Single increment per attempt: the gate INCR is the failure record.
    assert redis.record_login_failure.call_count == burst_size
    for response in responses:
        if response.status_code == 429:
            assert response.headers["Retry-After"] == "900"
            assert response.json() == {"detail": auth.RATE_LIMITED_DETAIL}


def _unconfigured_redis() -> UpstashRedisClient:
    """
    A real client with no credentials.

    Passing url="" to the constructor is not enough: it falls back to settings, so the
    absence of credentials has to be simulated where the client reads them.
    """
    import backend.redis_client as redis_module

    blanked = dataclasses.replace(settings, UPSTASH_REDIS_REST_URL="", UPSTASH_REDIS_REST_TOKEN="")
    with patch.object(redis_module, "settings", blanked):
        client = UpstashRedisClient()
    assert client.is_configured is False
    return client


def test_login_without_redis_returns_503(client: TestClient, configured_password: str) -> None:
    """No Redis means no session, and no cookie either."""
    unconfigured = _unconfigured_redis()

    with patch.object(auth_module, "get_redis_client", return_value=unconfigured):
        response = client.post("/auth/login", json={"password": OPERATOR_PASSWORD})

        assert response.status_code == 503
        assert response.json() == {"detail": auth.AUTH_UNAVAILABLE_DETAIL}
        assert "set-cookie" not in response.headers

        # Inside the patch on purpose. Run outside it, this probe reads the real
        # singleton, which is configured and reachable, and asserts on whatever the
        # mock Upstash server happened to be doing -- 401 once it is listening,
        # 503 only while it is still binding. That made the assertion below a
        # startup race rather than a check of the unconfigured-store path.
        probe = client.get(
            "/auth/session",
            headers={"Cookie": f"hulchul_session={auth.generate_session_token()}"},
        )
        assert probe.status_code == 503, "an unreachable session store must never validate a cookie"


def test_login_returns_503_when_redis_fails_mid_write(
    client: TestClient, configured_password: str
) -> None:
    """
    A correct password plus a Redis outage must be a 503, not an unhandled 500.

    The read path in require_session already distinguishes "cannot tell right now"
    from "no session"; the write path had no such guard, so a transport failure
    while persisting the new record propagated out of the login handler. The
    password was still verified, so this is the one place where a real outage
    could not be reported as the unavailability it is.
    """
    from backend.redis_client import UpstashRedisError

    broken = MagicMock(spec=UpstashRedisClient)
    broken.is_configured = True
    broken.get_login_failure_count = AsyncMock(return_value=0)
    # Atomic INCR-first gate consumes via record_login_failure, not GET.
    broken.record_login_failure = AsyncMock(return_value=1)
    broken.clear_login_failures = AsyncMock(return_value=None)
    broken.create_auth_session = AsyncMock(side_effect=UpstashRedisError("connection refused"))

    with patch.object(auth_module, "get_redis_client", return_value=broken):
        response = client.post("/auth/login", json={"password": OPERATOR_PASSWORD})

    assert response.status_code == 503
    assert response.json() == {"detail": auth.AUTH_UNAVAILABLE_DETAIL}
    assert "set-cookie" not in response.headers, "no cookie may outlive an unstored session"
    assert _session_keys() == []


def test_create_session_returns_none_when_the_redis_write_raises() -> None:
    """The unit-level contract behind the 503 above: a failed write yields None."""
    from backend.redis_client import UpstashRedisError

    broken = MagicMock(spec=UpstashRedisClient)
    broken.is_configured = True
    broken.create_auth_session = AsyncMock(side_effect=UpstashRedisError("connection refused"))

    with patch.object(auth_module, "get_redis_client", return_value=broken):
        assert _run(auth.create_session()) is None


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------

def test_verify_session_returns_none_when_redis_is_unconfigured() -> None:
    """Redis being unconfigured must read as "no valid session", never as valid."""
    unconfigured = _unconfigured_redis()

    with patch.object(auth_module, "get_redis_client", return_value=unconfigured):
        assert _run(auth.verify_session(auth.generate_session_token())) is None
        assert _run(auth.verify_session(None)) is None
        assert _run(auth.create_session()) is None


def test_verify_session_returns_none_when_redis_errors() -> None:
    """A Redis outage must never be mistaken for a valid session."""
    from backend.redis_client import UpstashRedisError

    broken = MagicMock(spec=UpstashRedisClient)
    broken.is_configured = True
    broken.get_auth_session = AsyncMock(side_effect=UpstashRedisError("connection refused"))

    with patch.object(auth_module, "get_redis_client", return_value=broken):
        assert _run(auth.verify_session(auth.generate_session_token())) is None


def test_protected_route_with_broken_redis_returns_503(client: TestClient) -> None:
    """A configured-but-unreachable Redis surfaces as 503, distinct from 401."""
    from backend.redis_client import UpstashRedisError

    broken = MagicMock(spec=UpstashRedisClient)
    broken.is_configured = True
    broken.get_auth_session = AsyncMock(side_effect=UpstashRedisError("connection refused"))
    cookie = f"hulchul_session={auth.generate_session_token()}"

    with patch.object(auth_module, "get_redis_client", return_value=broken):
        response = client.get("/tools", headers={"Cookie": cookie})

    assert response.status_code == 503


def test_verify_session_round_trip_returns_the_session() -> None:
    """verify_session resolves a stored token back to its Session."""
    token, _ = _store_session(ttl_seconds=3600)

    session = _run(auth.verify_session(token))

    assert session is not None
    assert session.sub == "operator"
    assert session.exp > int(time.time())


def _run(coro):
    """Drive a coroutine to completion from a synchronous test."""
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Public routes stay public
# ---------------------------------------------------------------------------

def test_health_endpoints_remain_reachable_without_a_cookie(client: TestClient) -> None:
    """Monitoring endpoints must never require a session."""
    with patch("backend.main.check_db_health", new=AsyncMock(return_value=True)):
        health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"

    root = client.get("/")
    assert root.status_code == 200

    redis_health = client.get("/health/redis")
    assert redis_health.status_code == 200


def test_public_routes_carry_no_session_dependency(client: TestClient) -> None:
    """The monitoring routes skip the guard; every agent and tools route must have it."""
    public_paths = {"/", "/health", "/health/browser", "/health/redis"}
    guarded_prefixes = ("/agent", "/tools")

    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        dependant = getattr(route, "dependant", None)
        has_guard = bool(dependant and dependant.dependencies)

        if route.path in public_paths:
            assert not has_guard, f"{route.path} must stay reachable without a session"
        elif route.path.startswith(guarded_prefixes):
            assert has_guard, f"{route.path} must require a session"
        elif route.path == "/auth/session":
            assert has_guard, "/auth/session must require a session"


def test_api_docs_exist_only_behind_an_explicit_opt_in(client: TestClient) -> None:
    """The docs routes bypass the guard, so they may only be mounted when asked for."""
    registered = {
        route.path for route in app.routes if getattr(route, "path", None) in DOCS_PATHS
    }

    assert registered == (DOCS_PATHS if settings.ENABLE_API_DOCS else set())

    for path in sorted(DOCS_PATHS - registered):
        response = client.get(path)
        assert response.status_code == 404, f"{path} must not be reachable"
        assert "openapi" not in response.text.lower(), f"{path} leaked the schema"


def test_api_docs_are_disabled_by_default() -> None:
    """The shipped default must leave the docs off, so production needs no opt-out."""
    from backend.config import Settings

    docs_default = next(f.default for f in dataclasses.fields(Settings) if f.name == "ENABLE_API_DOCS")
    assert docs_default is False


# ---------------------------------------------------------------------------
# CORS allowlist
# ---------------------------------------------------------------------------

def test_configured_origins_contain_no_wildcard() -> None:
    """The allowlist must be exact origins: no bare `*`, no host globs."""
    origins = get_allowed_origins()

    assert origins, "the allowlist must not be empty"
    assert "*" not in origins
    for origin in origins:
        assert "*" not in origin, f"{origin} is a glob, not an exact origin"
        assert origin.startswith(("http://", "https://"))


def test_cors_origins_default_drops_the_wildcard() -> None:
    """The shipped default must not contain `*` or an unsupported `*.pages.dev` glob."""
    from backend.config import Settings

    cors_default = next(f.default for f in dataclasses.fields(Settings) if f.name == "CORS_ORIGINS")
    assert cors_default == (
        "http://localhost:3051,http://127.0.0.1:3051,https://hulchul-frontend.sufiyanx.workers.dev"
    )


def test_cors_middleware_is_narrowed_and_keeps_credentials() -> None:
    """Methods and headers are explicit lists; credentials stay on for the cookie."""
    from starlette.middleware.cors import CORSMiddleware

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    options = cors[0].kwargs

    assert options["allow_credentials"] is True
    assert options["allow_methods"] == ["GET", "POST", "PATCH", "OPTIONS"]
    assert options["allow_headers"] == ["Content-Type", "Accept", "Cookie"]
    assert all(origin != "*" for origin in options["allow_origins"])


def test_cors_reflects_only_allowlisted_origins(client: TestClient) -> None:
    """An unlisted origin gets no allow-origin header; a listed one does."""
    allowed = client.get("/health", headers={"Origin": "http://localhost:3051"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3051"

    denied = client.get("/health", headers={"Origin": "https://attacker.example"})
    assert "access-control-allow-origin" not in denied.headers


# ---------------------------------------------------------------------------
# Redis wire format
# ---------------------------------------------------------------------------

def test_session_commands_serialise_every_argument_as_a_string() -> None:
    """The Upstash REST client stringifies args; session records must not smuggle ints."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"result": "OK"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    redis = UpstashRedisClient(url="https://example.upstash.io", token="t", http_client=http_client)

    payload = {"sub": "operator", "iat": 1750000000, "exp": 1750086400}
    assert _run(redis.create_auth_session("deadbeef", payload, 86400)) is True

    command = captured[0]
    assert command["url"] == "https://example.upstash.io"
    assert command["body"] == [
        "SET",
        "hulchul:auth:session:deadbeef",
        json.dumps(payload),
        "EX",
        "86400",
    ]
    assert all(isinstance(arg, str) for arg in command["body"])


def test_login_failure_counter_uses_incr_and_expire() -> None:
    """The rate-limit counter is INCR + EXPIRE, as the auth contract specifies."""
    captured: list[list] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"result": 3})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    redis = UpstashRedisClient(url="https://example.upstash.io", token="t", http_client=http_client)

    assert _run(redis.record_login_failure("203.0.113.9", 900)) == 3
    assert captured == [
        ["INCR", "hulchul:auth:login_fail:203.0.113.9"],
        ["EXPIRE", "hulchul:auth:login_fail:203.0.113.9", "900"],
    ]


def test_login_failure_counter_treats_a_missing_key_as_zero() -> None:
    """A first-time IP must not be counted as failing."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": None})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    redis = UpstashRedisClient(url="https://example.upstash.io", token="t", http_client=http_client)

    assert _run(redis.get_login_failure_count("203.0.113.9")) == 0