import asyncio
import json
import logging
from typing import Any, Dict, Optional, Union
import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


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
        self.url = (url or settings.UPSTASH_REDIS_REST_URL).rstrip("/")
        self.token = token or settings.UPSTASH_REDIS_REST_TOKEN
        self._external_client = http_client is not None
        self._client = http_client
        self._loop = None

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.token)

    async def _get_client(self) -> httpx.AsyncClient:
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
        """Write approval-pending state to Redis."""
        key = f"hulchul:approval:{run_id}"
        # Ensure status is awaiting_approval
        payload = dict(data)
        payload["run_id"] = run_id
        payload["status"] = "awaiting_approval"
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

    async def set_approval_decision(self, run_id: str, decision: str) -> bool:
        """Record human decision ('approved' or 'rejected') for an awaiting run."""
        norm_decision = decision.strip().lower()
        if norm_decision not in ("approved", "rejected"):
            raise ValueError(f"Decision must be 'approved' or 'rejected', got '{decision}'")

        key = f"hulchul:decision:{run_id}"
        res = await self.execute_command("SET", key, norm_decision, "EX", 86400)
        return res == "OK"

    async def get_approval_decision(self, run_id: str) -> Optional[str]:
        """Poll for human approval decision ('approved', 'rejected', or None)."""
        key = f"hulchul:decision:{run_id}"
        res = await self.execute_command("GET", key)
        if res in ("approved", "rejected"):
            return res
        return None

    async def clear_approval(self, run_id: str) -> bool:
        """Clean up approval request and decision keys after resolution."""
        k1 = f"hulchul:approval:{run_id}"
        k2 = f"hulchul:decision:{run_id}"
        await self.execute_command("DEL", k1)
        await self.execute_command("DEL", k2)
        return True

    async def close(self) -> None:
        """Close the underlying HTTP client if locally owned."""
        if not self._external_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


_global_redis_client: Optional[UpstashRedisClient] = None


def get_redis_client() -> UpstashRedisClient:
    global _global_redis_client
    if _global_redis_client is None:
        _global_redis_client = UpstashRedisClient()
    return _global_redis_client
