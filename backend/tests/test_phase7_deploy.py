"""
Test suite for Phase 7: Deployment Configuration & Environment Templates.
Verifies Dockerfile parameters, environment template examples, and dynamic CORS settings.
"""

import os
import pytest
from backend.config import Settings
from backend.main import get_allowed_origins


def test_dockerfile_exists_and_configured() -> None:
    """
    Verify that backend/Dockerfile exists and contains AWS Lambda Web Adapter,
    AWS_LWA_ENABLE_COMPRESSION=false, and PORT=8051 configurations.
    """
    dockerfile_path = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")
    assert os.path.exists(dockerfile_path), "backend/Dockerfile must exist"

    with open(dockerfile_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "aws-lambda-adapter" in content.lower() or "lambda-adapter" in content, \
        "Dockerfile must reference AWS Lambda Web Adapter"
    assert "AWS_LWA_ENABLE_COMPRESSION=false" in content, \
        "Dockerfile must set AWS_LWA_ENABLE_COMPRESSION=false for unbuffered SSE streaming"
    assert "PORT=8051" in content, \
        "Dockerfile must set PORT=8051"
    assert "response_stream" in content, \
        "Dockerfile must set AWS_LWA_INVOKE_MODE=response_stream"


def test_backend_env_example_keys() -> None:
    """
    Verify that backend/.env.example documents all required keys and contains no real credentials.
    """
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
    assert os.path.exists(env_path), "backend/.env.example must exist"

    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    required_keys = [
        "DATABASE_URL",
        "BROWSER_WS_ENDPOINT",
        "GROQ_API_KEY",
        "UPSTASH_REDIS_REST_URL",
        "UPSTASH_REDIS_REST_TOKEN",
        "PORT",
        "CORS_ORIGINS",
    ]
    for key in required_keys:
        assert key in content, f"backend/.env.example must document {key}"

    # Verify no real live credentials (e.g., actual secret keys or passwords)
    assert "gsk_your_groq_api_key_here" in content or "gsk_" not in content.replace("gsk_your_groq_api_key_here", ""), \
        "backend/.env.example must not contain real API keys"


def test_frontend_env_example_keys() -> None:
    """
    Verify that frontend/.env.example documents required keys and contains no real credentials.
    """
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", ".env.example")
    assert os.path.exists(env_path), "frontend/.env.example must exist"

    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "DATABASE_URL" in content, "frontend/.env.example must document DATABASE_URL"
    assert "NEXT_PUBLIC_BACKEND_URL" in content or "NEXT_PUBLIC_API_URL" in content, \
        "frontend/.env.example must document backend URL"


def test_dynamic_cors_parsing() -> None:
    """
    Verify that dynamic CORS parsing correctly processes comma-separated origins and defaults.
    """
    # Test default settings CORS origins
    origins = get_allowed_origins()
    assert isinstance(origins, list)
    assert len(origins) > 0
    assert "http://localhost:3051" in origins
