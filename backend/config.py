"""
Configuration management module for FastAPI backend settings.
Loads environment variables and provides structured typed settings.
"""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Ensure .env is loaded from the backend directory
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


@dataclass(frozen=True)
class Settings:
    """
    Immutable dataclass holding application configuration settings loaded from environment variables.
    """
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    BROWSER_WS_ENDPOINT: str = os.getenv("BROWSER_WS_ENDPOINT", "")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    UPSTASH_REDIS_REST_URL: str = os.getenv("UPSTASH_REDIS_REST_URL", "")
    UPSTASH_REDIS_REST_TOKEN: str = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    NEXT_PUBLIC_API_URL: str = os.getenv("NEXT_PUBLIC_API_URL", "http://localhost:3051")
    PORT: int = int(os.getenv("PORT", "8051"))
    # Exact origins only. A bare "*" is invalid alongside allow_credentials and a
    # literal "https://*.pages.dev" is silently ignored by Starlette, so both used
    # to be dead config that made the wildcard the real allowlist.
    CORS_ORIGINS: str = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3051,http://127.0.0.1:3051,https://hulchul-frontend.sufiyanx.workers.dev"
    )

    # Auth settings. AUTH_PASSWORD_HASH is an argon2id PHC string; leaving it unset
    # disables login entirely (503) rather than falling back to any default secret.
    AUTH_PASSWORD_HASH: str = os.getenv("AUTH_PASSWORD_HASH", "")
    AUTH_SESSION_TTL_SECONDS: int = int(os.getenv("AUTH_SESSION_TTL_SECONDS", "86400"))
    # /docs, /redoc and /openapi.json sit outside the session guard and publish
    # every route to anonymous callers, so they stay off unless asked for.
    ENABLE_API_DOCS: bool = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"
    SIMULATE_FAILURE_AFTER: Optional[int] = (
        int(os.getenv("SIMULATE_FAILURE_AFTER"))
        if os.getenv("SIMULATE_FAILURE_AFTER")
        else None
    )

    # CDP / Browser settings
    BROWSER_CONNECT_TIMEOUT_MS: int = int(os.getenv("BROWSER_CONNECT_TIMEOUT_MS", "30000"))

    # Database pool settings
    DB_POOL_MIN_SIZE: int = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
    DB_POOL_MAX_SIZE: int = int(os.getenv("DB_POOL_MAX_SIZE", "10"))
    DB_POOL_MAX_INACTIVE_LIFETIME: float = float(os.getenv("DB_POOL_MAX_INACTIVE_LIFETIME", "180.0"))

    # Agent & LLM settings
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    MAX_AGENT_ITERATIONS: int = int(os.getenv("MAX_AGENT_ITERATIONS", "30"))
    DEFAULT_APPROVAL_THRESHOLD: float = float(os.getenv("DEFAULT_APPROVAL_THRESHOLD", "50000.0"))
    APPROVAL_TIMEOUT_SECONDS: float = float(os.getenv("APPROVAL_TIMEOUT_SECONDS", "120.0"))
    PAUSE_TIMEOUT_SECONDS: float = float(os.getenv("PAUSE_TIMEOUT_SECONDS", "300.0"))


settings = Settings()
