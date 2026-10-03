"""
Cookie-session authentication for the single-operator backend.

Sessions are opaque random tokens and Redis only ever receives a SHA-256 digest of
one, so a Redis dump cannot be replayed as a live session. Every failure path here
fails closed: anything not positively confirmed against Redis is unauthenticated.
"""

import hashlib
import ipaddress
import json
import logging
import re
import secrets
import time
from enum import Enum
from typing import NamedTuple, Optional, Tuple

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError
from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from backend.config import settings
from backend.redis_client import UpstashRedisError, get_redis_client

logger = logging.getLogger(__name__)

SESSION_SUBJECT = "operator"
SESSION_COOKIE_NAME = "hulchul_session"
SESSION_COOKIE_HOST_NAME = "__Host-hulchul_session"

LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 900
LOGIN_RETRY_AFTER_SECONDS = 900

UNAUTHENTICATED_DETAIL = "Not authenticated"
AUTH_UNAVAILABLE_DETAIL = "Authentication is temporarily unavailable"
INVALID_CREDENTIALS_DETAIL = "Invalid credentials"
RATE_LIMITED_DETAIL = "Too many failed login attempts"

# Bucketing key material feeds Redis keys, so it stays bounded and
# charset-restricted even though the trusted source below is AWS-set.
_UNSAFE_IP_CHARS = re.compile(r"[^A-Za-z0-9.:_-]")
MAX_IP_BUCKET_LENGTH = 64

# Fail-closed bucket used when the Lambda adapter context header is absent or
# malformed (local dev, unit tests). A fixed key on purpose: request.client.host
# is loopback behind the adapter, so keying on it would silently share one
# bucket across all clients while looking per-IP.
GLOBAL_LOGIN_BUCKET = "global"

_password_hasher = PasswordHasher()


class Session(BaseModel):
    """Authenticated session identity. Single-operator app, so there is no user table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub: str
    exp: int


class IssuedSession(NamedTuple):
    """A freshly minted session; the raw token exists only to be written to the cookie."""

    token: str
    session: Session


class _LookupStatus(Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


def generate_session_token() -> str:
    """32 cryptographically random bytes, base64url unpadded (~43 chars)."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Lowercase SHA-256 hex digest; the only session representation ever persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _trusted_source_ip(request: Request) -> Optional[str]:
    """
    Extract the AWS-set client IP from the Lambda adapter context header.

    The adapter overwrites ``x-amzn-request-context`` with the real Lambda event
    JSON, so a client-forged value never survives. Prefer the payload v2.0 shape
    ``requestContext.http.sourceIp``, fall back to the v1 shape
    ``requestContext.identity.sourceIp``. Return None when the header is absent,
    malformed, or not a real IP.
    """
    raw_context = request.headers.get("x-amzn-request-context")
    if not raw_context:
        return None
    try:
        event = json.loads(raw_context)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return None
    candidate: object = None
    http = request_context.get("http")
    if isinstance(http, dict):
        candidate = http.get("sourceIp")
    if not candidate:
        identity = request_context.get("identity")
        if isinstance(identity, dict):
            candidate = identity.get("sourceIp")
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    candidate = candidate.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def client_ip(request: Request) -> str:
    """
    Best-effort client identity used *only* to bucket the login rate limiter.

    The bucket key is the trusted ``sourceIp`` from the ``x-amzn-request-context``
    header set by AWS infrastructure. ``X-Forwarded-For`` passes through the
    adapter verbatim (fully attacker-controlled) and ``request.client.host`` is
    loopback behind the adapter, so neither is consulted here. Without a usable
    context header the limiter fails closed into a single coarse GLOBAL bucket.

    X-Forwarded-For is client-controlled, so it must never gate authorization.
    """
    source_ip = _trusted_source_ip(request)
    candidate = source_ip if source_ip is not None else GLOBAL_LOGIN_BUCKET
    return _UNSAFE_IP_CHARS.sub("_", candidate)[:MAX_IP_BUCKET_LENGTH]


def is_https_request(request: Request) -> bool:
    """
    Decide whether to issue a `__Host-` prefixed, Secure cookie.

    TLS is terminated before this app (AWS Lambda Web Adapter in front of uvicorn),
    so the ASGI scheme stays http on real HTTPS traffic and the proxy hop hint is
    the only signal. Getting this wrong in the false direction would strip Secure
    from a cookie the browser then sends in the clear.
    """
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto.split(",")[0].strip().lower() == "https"


def session_cookie_name(secure: bool) -> str:
    """`__Host-` is rejected by browsers on plain-HTTP localhost, so dev needs its own name."""
    return SESSION_COOKIE_HOST_NAME if secure else SESSION_COOKIE_NAME


def _apply_session_cookie(response: Response, value: str, max_age: int, secure: bool) -> None:
    """Single definition of the session cookie so login and logout cannot drift apart."""
    response.set_cookie(
        key=session_cookie_name(secure),
        value=value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def set_session_cookie(response: Response, token: str, secure: bool) -> None:
    """Write the session cookie. Domain is deliberately omitted: `__Host-` forbids it."""
    _apply_session_cookie(response, token, settings.AUTH_SESSION_TTL_SECONDS, secure)


def clear_session_cookie(response: Response, secure: bool) -> None:
    """Expire the session cookie using the same attributes it was issued with."""
    _apply_session_cookie(response, "", 0, secure)


def read_session_token(request: Request) -> Optional[str]:
    """Read the raw token, preferring the HTTPS cookie name over the dev one."""
    for name in (SESSION_COOKIE_HOST_NAME, SESSION_COOKIE_NAME):
        token = request.cookies.get(name)
        if token:
            return token
    return None


def _parse_stored_session(payload: object) -> Optional[Session]:
    """
    Rebuild a Session from a Redis record.

    Redis TTL is the primary expiry mechanism but is not trusted on its own: the
    stored `exp` is re-checked here, and anything malformed is treated as invalid.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("sub") != SESSION_SUBJECT:
        return None
    try:
        exp = int(payload["exp"])
    except (KeyError, TypeError, ValueError):
        return None
    if exp <= int(time.time()):
        return None
    return Session(sub=SESSION_SUBJECT, exp=exp)


async def _lookup(raw_token: Optional[str]) -> Tuple[Optional[Session], _LookupStatus]:
    """
    Resolve a raw token to a session, distinguishing "not authenticated" from
    "cannot tell right now" so the caller can pick 401 vs 503.
    """
    if not raw_token:
        return None, _LookupStatus.INVALID
    redis = get_redis_client()
    if not redis.is_configured:
        return None, _LookupStatus.UNAVAILABLE
    try:
        payload = await redis.get_auth_session(hash_session_token(raw_token))
    except UpstashRedisError as exc:
        logger.error(f"Session lookup failed: {exc}")
        return None, _LookupStatus.UNAVAILABLE
    if payload is None:
        return None, _LookupStatus.INVALID
    session = _parse_stored_session(payload)
    if session is None:
        return None, _LookupStatus.INVALID
    return session, _LookupStatus.VALID


async def verify_session(raw_token: Optional[str]) -> Optional[Session]:
    """
    Validate a raw token against Redis. Never raises and never returns a session
    that was not positively confirmed.
    """
    session, _ = await _lookup(raw_token)
    return session


async def require_session(request: Request) -> Session:
    """
    FastAPI dependency guarding every non-monitoring route.

    Runs per request inside the handler's dependency graph and re-verifies the
    cookie against Redis every time; the cookie alone is never trusted.
    """
    session, lookup = await _lookup(read_session_token(request))
    if lookup is _LookupStatus.UNAVAILABLE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AUTH_UNAVAILABLE_DETAIL)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=UNAUTHENTICATED_DETAIL)
    return session


async def create_session() -> Optional[IssuedSession]:
    """
    Mint a session and persist only its digest under a fixed TTL.

    Returns None when the session could not be stored, so a cookie is never
    handed out for a session that does not exist server-side.
    """
    redis = get_redis_client()
    if not redis.is_configured:
        logger.error("Refusing to issue a session: Redis is not configured")
        return None

    token = generate_session_token()
    issued_at = int(time.time())
    payload = {
        "sub": SESSION_SUBJECT,
        "iat": issued_at,
        "exp": issued_at + settings.AUTH_SESSION_TTL_SECONDS,
    }
    try:
        stored = await redis.create_auth_session(
            hash_session_token(token), payload, settings.AUTH_SESSION_TTL_SECONDS
        )
    except UpstashRedisError as exc:
        # execute_command raises on a transport failure or a non-200, which is the
        # same "cannot confirm" case as a rejected write. Left unhandled it
        # surfaced as a 500 from /auth/login instead of the 503 the caller maps
        # `None` to, so a transient Redis blip looked like an application bug.
        logger.error(f"Refusing to issue a session: Redis write failed: {exc}")
        return None
    if not stored:
        logger.error("Refusing to issue a session: Redis rejected the session record")
        return None
    return IssuedSession(token=token, session=Session(sub=SESSION_SUBJECT, exp=payload["exp"]))


async def destroy_session(raw_token: Optional[str]) -> bool:
    """
    Revoke a session record. Returns True when logout may proceed to 204.

    Logout stays idempotent: a missing or already-deleted key (DEL -> 0) counts
    as revoked. Returns False only when revocation cannot be confirmed -- Redis
    unconfigured while a token was presented, or the delete raised -- so the
    caller can fail closed with a 503 instead of clearing the cookie on top of
    a still-valid session.
    """
    if not raw_token:
        return True
    redis = get_redis_client()
    if not redis.is_configured:
        logger.error("Session revocation failed: Redis is not configured")
        return False
    try:
        await redis.delete_auth_session(hash_session_token(raw_token))
    except UpstashRedisError as exc:
        logger.error(f"Session revocation failed: {exc}")
        return False
    return True


async def verify_password(candidate: str) -> bool:
    """
    Verify a password against AUTH_PASSWORD_HASH.

    An unset hash is a configuration failure, not a credential failure, and raises
    503 so login can never succeed with an implicit default secret. A wrong
    password and an unusable stored hash both return False so the client cannot
    tell them apart.
    """
    stored_hash = settings.AUTH_PASSWORD_HASH
    if not stored_hash or not stored_hash.strip():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AUTH_UNAVAILABLE_DETAIL)

    try:
        # argon2id is deliberately CPU/memory expensive, so it runs off the event
        # loop instead of stalling every other in-flight request.
        return await anyio.to_thread.run_sync(_password_hasher.verify, stored_hash, candidate)
    except VerificationError:
        # Base class of VerifyMismatchError: the password simply did not match.
        return False
    except InvalidHash:
        # A stored hash argon2 cannot parse at all -- a bcrypt string pasted into
        # AUTH_PASSWORD_HASH, a truncated PHC value, a stray newline. These are
        # ValueError, *not* VerificationError, so they used to escape this handler
        # and surface as an unhandled 500 from /auth/login. Treated as a
        # credential failure, so a wrong password and an unusable stored hash stay
        # indistinguishable to the caller.
        return False


async def enforce_login_rate_limit(ip: str) -> None:
    """
    Atomically consume one failure-budget slot before any hashing runs, so
    argon2 cost cannot be amplified into a free CPU-exhaustion vector.

    The gate issues a single server-atomic INCR and rejects when the post-INCR
    count exceeds the budget. The INCR *is* the failure record, so callers must
    not record a second time on a 401 -- that would double-count and halve the
    sequential budget. Blocked (over-budget) increments carry no EXPIRE, so the
    window stays anchored at the last counted failure instead of sliding
    indefinitely under sustained blocked probes. Any unconfirmed counter state
    (transport failure or corrupt count) fails closed with 503 before hashing.
    """
    redis = get_redis_client()
    if not redis.is_configured:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AUTH_UNAVAILABLE_DETAIL)
    try:
        failures = await redis.record_login_failure(ip, LOGIN_WINDOW_SECONDS, LOGIN_MAX_FAILURES)
    except UpstashRedisError as exc:
        logger.error(f"Login rate limit lookup failed: {exc}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AUTH_UNAVAILABLE_DETAIL)

    try:
        count = int(failures)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.error(f"Login rate limit counter corrupt, failing closed: {failures!r}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AUTH_UNAVAILABLE_DETAIL)

    if count > LOGIN_MAX_FAILURES:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMITED_DETAIL,
            headers={"Retry-After": str(LOGIN_RETRY_AFTER_SECONDS)},
        )


async def clear_login_failures(ip: str) -> None:
    """Reset the failure counter once a correct password has been presented."""
    redis = get_redis_client()
    try:
        await redis.clear_login_failures(ip)
    except UpstashRedisError as exc:
        logger.warning(f"Failed to clear login failure counter: {exc}")