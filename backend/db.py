import asyncio
import logging
from typing import AsyncGenerator, Optional
import asyncpg
from backend.config import settings

logger = logging.getLogger(__name__)

_db_pool: Optional[asyncpg.Pool] = None


async def init_db_pool() -> asyncpg.Pool:
    """Initialize the asyncpg connection pool if not already initialized for current loop."""
    global _db_pool
    current_loop = asyncio.get_running_loop()
    if (
        _db_pool is None
        or getattr(_db_pool, "_closed", True)
        or getattr(_db_pool, "_loop", None) is not current_loop
    ):
        if not settings.DATABASE_URL:
            raise ValueError("DATABASE_URL is not set in environment or .env")
        logger.info("Initializing asyncpg connection pool...")
        _db_pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=settings.DB_POOL_MIN_SIZE,
            max_size=settings.DB_POOL_MAX_SIZE,
            max_inactive_connection_lifetime=settings.DB_POOL_MAX_INACTIVE_LIFETIME,
        )
        logger.info("asyncpg connection pool initialized successfully.")
    return _db_pool


async def get_db_pool() -> asyncpg.Pool:
    """Get the current asyncpg pool, initializing if necessary."""
    global _db_pool
    current_loop = asyncio.get_running_loop()
    if (
        _db_pool is None
        or getattr(_db_pool, "_closed", True)
        or getattr(_db_pool, "_loop", None) is not current_loop
    ):
        return await init_db_pool()
    return _db_pool


async def close_db_pool() -> None:
    """Close all connections in the asyncpg pool."""
    global _db_pool
    if _db_pool is not None:
        logger.info("Closing asyncpg connection pool...")
        await _db_pool.close()
        _db_pool = None
        logger.info("asyncpg connection pool closed.")


async def get_db_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Dependency for acquiring a database connection from the pool and releasing it cleanly."""
    pool = await get_db_pool()
    async with pool.acquire() as connection:
        yield connection


async def check_db_health() -> bool:
    """Execute a lightweight probe query to verify database connectivity."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval("SELECT 1")
            return val == 1
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False
