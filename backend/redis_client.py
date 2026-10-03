import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional, Union
import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

# Dedicated namespace for the auth feature; never reused by run/pause/approval keys.
AUTH_SESSION_KEY_PREFIX = "hulchul:auth:session:"
AUTH_LOGIN_FAIL_KEY_PREFIX = "hulchul:auth:login_fail:"


class UpstashRedisError(Exception):
    """Raised when an operation on Upstash Redis fails."""
    pass


class UpstashRedisClient:
    """
    Production-grade async client for Upstash Redis via its REST API.
    Provides session state tracking, pause/resume flags, and human-in-the-loop approval gates.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        """Initialize the Upstash Redis client with credentials and HTTP client."""
        self.url = (url or settings.UPSTASH_REDIS_REST_URL).rstrip("/")
        self.token = token or settings.UPSTASH_REDIS_REST_TOKEN
        self._external_client = http_client is not None
        self._client = http_client
        self._loop = None

    @property
    def is_configured(self) -> bool:
        """Check if Upstash Redis credentials (URL and token) are provided."""
        return bool(self.url and self.token)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or initialize the underlying httpx AsyncClient tied to the active event loop."""
        if self._external_client and self._client:
            return self._client
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if self._client is None or self._client.is_closed or self._loop != curr_loop:
            if self._client and not self._client.is_closed and not self._external_client:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._loop = curr_loop
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def execute_command(self, *args: Any) -> Any:
        """
        Executes a raw Redis command using Upstash REST array serialization.
        Example: execute_command("SET", "key", "val")
        """
        if not self.is_configured:
            logger.warning("Upstash Redis is not configured; skipping execute_command")
            return None

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        cmd_args = [str(arg) for arg in args]

        try:
            client = await self._get_client()
            resp = await client.post(self.url, headers=headers, json=cmd_args)
            if resp.status_code != 200:
                logger.error(f"Upstash Redis error HTTP {resp.status_code}: {resp.text}")
                raise UpstashRedisError(f"Redis command failed with status {resp.status_code}: {resp.text}")

            data = resp.json()
            if "error" in data:
                logger.error(f"Upstash Redis command error: {data['error']}")
                raise UpstashRedisError(data["error"])

            return data.get("result")
        except httpx.RequestError as e:
            logger.error(f"Network error communicating with Upstash Redis: {e}")
            raise UpstashRedisError(f"Network error: {e}") from e

    async def ping(self) -> bool:
        """Health probe checking Upstash Redis connectivity."""
        try:
            result = await self.execute_command("PING")
            return result == "PONG"
        except Exception as e:
            logger.warning(f"Redis ping check failed: {e}")
            return False

    async def set_session_state(
        self,
        run_id: str,
        state: Dict[str, Any],
        ttl_seconds: int = 86400,
    ) -> bool:
        """Persist active agent session state (step index, run_id, goal, etc.)."""
        key = f"hulchul:run:{run_id}"
        serialized = json.dumps(state)
        res = await self.execute_command("SET", key, serialized, "EX", ttl_seconds)
        return res == "OK"

    async def get_session_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve active agent session state."""
        key = f"hulchul:run:{run_id}"
        raw = await self.execute_command("GET", key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set_pause_flag(self, run_id: str, paused: bool) -> bool:
        """Set pause flag for an agent run (True = paused, False = running)."""
        key = f"hulchul:pause:{run_id}"
        val = "paused" if paused else "running"
        res = await self.execute_command("SET", key, val, "EX", 86400)
        return res == "OK"

    async def get_pause_flag(self, run_id: str) -> bool:
        """Check if an agent run is currently paused."""
        key = f"hulchul:pause:{run_id}"
        val = await self.execute_command("GET", key)
        return val == "paused"

    async def set_approval_pending(
        self,
        run_id: str,
        data: Dict[str, Any],
        ttl_seconds: int = 86400,
    ) -> bool:
        """Write approval-pending state to Redis. Generates and persists a nonce when missing."""
        key = f"hulchul:approval:{run_id}"
        # Ensure status is awaiting_approval
        payload = dict(data)
        payload["run_id"] = run_id
        payload["status"] = "awaiting_approval"
        if not payload.get("nonce"):
            payload["nonce"] = uuid.uuid4().hex[:12]
        res = await self.execute_command("SET", key, json.dumps(payload), "EX", ttl_seconds)
        return res == "OK"

    async def get_approval_pending(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve current approval request details for a run."""
        key = f"hulchul:approval:{run_id}"
        raw = await self.execute_command("GET", key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def set_approval_decision(self, run_id: str, decision: str, nonce: Optional[str] = None) -> bool:
        """Record human decision ('approved' or 'rejected') for an awaiting run with optional request nonce."""
        norm_decision = decision.strip().lower()
        if norm_decision not in ("approved", "rejected"):
            raise ValueError(f"Decision must be 'approved' or 'rejected', got '{decision}'")

        key = f"hulchul:decision:{run_id}"
        if nonce:
            payload = json.dumps({"decision": norm_decision, "nonce": nonce})
            res = await self.execute_command("SET", key, payload, "EX", 86400)
        else:
            res = await self.execute_command("SET", key, norm_decision, "EX", 86400)
        return res == "OK"

    async def get_approval_decision(self, run_id: str) -> Optional[str]:
        """Poll for human approval decision ('approved', 'rejected', or None)."""
        key = f"hulchul:decision:{run_id}"
        res = await self.execute_command("GET", key)
        if not res:
            return None
        if isinstance(res, str):
            try:
                parsed = json.loads(res)
                if isinstance(parsed, dict) and "decision" in parsed:
                    return parsed["decision"]
            except Exception:
                pass
            if res in ("approved", "rejected"):
                return res
        return None

    async def get_approval_decision_record(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve detailed approval decision record with nonce. Fail closed on nonce-less records."""
        key = f"hulchul:decision:{run_id}"
        res = await self.execute_command("GET", key)
        if not res:
            return None
        if isinstance(res, str):
            try:
                parsed = json.loads(res)
                if isinstance(parsed, dict) and "decision" in parsed:
                    if not parsed.get("nonce"):
                        return None
                    return parsed
            except Exception:
                pass
            # Bare-string legacy format carries no nonce: fail closed, do not accept.
            return None
        return None

    async def clear_approval(self, run_id: str) -> bool:
        """Clean up approval request and decision keys after resolution."""
        k1 = f"hulchul:approval:{run_id}"
        k2 = f"hulchul:decision:{run_id}"
        await self.execute_command("DEL", k1)
        await self.execute_command("DEL", k2)
        return True

    async def create_auth_session(
        self,
        token_hash: str,
        payload: Dict[str, Any],
        ttl_seconds: int,
    ) -> bool:
        """Persist a session record under the SHA-256 digest of its raw token."""
        key = f"{AUTH_SESSION_KEY_PREFIX}{token_hash}"
        res = await self.execute_command("SET", key, json.dumps(payload), "EX", ttl_seconds)
        return res == "OK"

    async def get_auth_session(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Fetch a session record by token digest. None when absent or unreadable."""
        key = f"{AUTH_SESSION_KEY_PREFIX}{token_hash}"
        raw = await self.execute_command("GET", key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def delete_auth_session(self, token_hash: str) -> bool:
        """Revoke a session record by token digest. Idempotent."""
        key = f"{AUTH_SESSION_KEY_PREFIX}{token_hash}"
        res = await self.execute_command("DEL", key)
        return bool(res)

    async def get_login_failure_count(self, ip: str) -> int:
        """Read the failed-login counter for an IP bucket. Missing key counts as zero.

        Any present-but-unparseable value is treated as unconfirmed and raises
        rather than coercing to 0 (fail closed: a corrupt counter must never
        read as "no failures").
        """
        key = f"{AUTH_LOGIN_FAIL_KEY_PREFIX}{ip}"
        raw = await self.execute_command("GET", key)
        if raw is None:
            return 0
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.error(f"Corrupt login failure counter for {ip!r}: {raw!r}")
            raise UpstashRedisError(f"Corrupt login failure counter for {ip!r}")

    async def record_login_failure(self, ip: str, window_seconds: int, max_failures: int) -> int:
        """
        Increment the failed-login counter, re-arming its window only while
        the post-INCR count is still within budget.

        Blocked (over-budget) increments are plain INCR with no EXPIRE, so
        sustained blocked probes cannot extend the lockout indefinitely (fixed
        expiry anchored at the last counted failure). A fresh key (count 1)
        always gets EXPIRE so a non-positive budget can never create an
        immortal key. A non-integer INCR result is unconfirmed and raises
        instead of coercing to 0.
        """
        key = f"{AUTH_LOGIN_FAIL_KEY_PREFIX}{ip}"
        count = await self.execute_command("INCR", key)
        try:
            failures = int(count)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.error(f"Corrupt INCR result for login failure counter {ip!r}: {count!r}")
            raise UpstashRedisError(f"Corrupt INCR result for login failure counter {ip!r}")
        if failures <= max_failures or failures == 1:
            await self.execute_command("EXPIRE", key, window_seconds)
        return failures

    async def clear_login_failures(self, ip: str) -> None:
        """Reset the failed-login counter after a successful authentication."""
        key = f"{AUTH_LOGIN_FAIL_KEY_PREFIX}{ip}"
        await self.execute_command("DEL", key)

    async def close(self) -> None:
        """Close the underlying HTTP client if locally owned."""
        if not self._external_client and self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception as e:
                # The client can be bound to an event loop that has already closed
                # (shared singleton reused across loops). Discard it either way so a
                # dead client is never handed to the next caller.
                logger.warning(f"Upstash Redis client close failed, discarding it: {e}")
            finally:
                self._client = None


_global_redis_client: Optional[UpstashRedisClient] = None


def get_redis_client() -> UpstashRedisClient:
    """Retrieve or create the global singleton UpstashRedisClient instance."""
    global _global_redis_client
    if _global_redis_client is None:
        _global_redis_client = UpstashRedisClient()
    return _global_redis_client
