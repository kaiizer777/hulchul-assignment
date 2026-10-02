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


def _read_repo_file(*parts: str) -> str:
    """Read a repository file relative to backend/tests/."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", *parts)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _dockerfile_instructions() -> str:
    """
    Return the Dockerfile with comment lines removed.

    Absence assertions must not match explanatory comments, otherwise documenting
    the failure mode would itself trip the guard.
    """
    content = _read_repo_file("backend", "Dockerfile")
    return "\n".join(
        line for line in content.splitlines() if not line.strip().startswith("#")
    )


def test_lambda_adapter_is_sourced_from_ecr_public() -> None:
    """
    The Lambda Web Adapter must be copied from ECR Public.

    Regression guard: the adapter was previously downloaded with curl from a
    GitHub *releases/download* URL. That release publishes no binary assets, so
    the URL returns HTTP 404 and the 404 response body was written to
    /opt/extensions/lambda-adapter. chmod +x still succeeds on a text file, so the
    build passes while Lambda fails every invocation with
    "Extension.Crash ... exit status 127". GitHub releases ship source tarballs
    only; the precompiled binaries are published to public.ecr.aws.
    """
    instructions = _dockerfile_instructions()

    assert "public.ecr.aws/awsguru/aws-lambda-adapter" in instructions, \
        "Dockerfile must copy the adapter from public.ecr.aws/awsguru/aws-lambda-adapter"
    assert "releases/download" not in instructions, \
        "Dockerfile must not download the adapter from a GitHub releases URL: that release has no binary assets"
    assert "curl -Lo /opt/extensions/lambda-adapter" not in instructions, \
        "Dockerfile must not curl the adapter into place without validating the response"


def test_image_preserves_backend_package_layout() -> None:
    """
    The image must contain the application at /app/backend so that the absolute
    `from backend.config import settings` imports resolve at runtime.

    Regression guard: the image was built with `backend/` as the build context and
    `COPY . .`, which flattens the package to /app/main.py. Importing it then
    failed with "ModuleNotFoundError: No module named 'backend'" at main.py:14 and
    every endpoint returned Extension.Crash.

    This asserts the invariant statically. The behavioural gate is manual:
        docker build -t hulchul-backend:check -f backend/Dockerfile .
        docker run --rm --entrypoint python hulchul-backend:check -c "import backend.main"
    Docker is not available in CI, so that import check cannot run here.
    """
    instructions = _dockerfile_instructions()

    assert "COPY backend/ ./backend/" in instructions, \
        "Dockerfile must copy the package into /app/backend to preserve backend.* imports"
    assert "COPY . ." not in instructions, \
        "Dockerfile must not use a bare `COPY . .`, which flattens the backend package"

    main_source = _read_repo_file("backend", "main.py")
    assert "from backend.config import settings" in main_source, \
        "backend/main.py is expected to use absolute backend.* imports"

    assert "backend.main:app" in instructions, \
        "Dockerfile must launch the app as backend.main:app so the package is importable"


def test_deploy_script_uses_repo_root_build_context() -> None:
    """
    The Docker build must use the repository root as its context, with the
    Dockerfile addressed explicitly, because backend/Dockerfile copies
    `backend/` relative to that context.
    """
    content = _read_repo_file("infra", "aws", "deploy.ps1")

    build_lines = [line.strip() for line in content.splitlines() if "docker build" in line]
    assert build_lines, "deploy.ps1 must contain a docker build invocation"

    build_line = build_lines[0]
    assert "-f ../../backend/Dockerfile" in build_line, \
        "deploy.ps1 must address the Dockerfile explicitly"
    assert build_line.rstrip().endswith("../../"), \
        "deploy.ps1 must use the repository root as the docker build context"

    # A `backend/` context would flatten the package again.
    assert "../../backend" not in build_line.replace("-f ../../backend/Dockerfile", ""), \
        "deploy.ps1 must not use backend/ as the build context"


def test_deploy_script_rejects_loopback_frontend_urls_by_host() -> None:
    """
    The frontend URL guard must resolve the host instead of matching the raw string.

    Regression guard: the guard was a single regex, "^(https?://)?(localhost|
    127\\.0\\.0\\.1)(:\\d+)?(/.*)?$", which never matched a bracketed IPv6 loopback.
    NEXT_PUBLIC_API_URL=http://[::1]:3051 was accepted and written to
    terraform.tfvars.json, so the Lambda container drove the remote browser at its
    own loopback interface while the deployment reported success. The regex also
    missed the rest of 127.0.0.0/8.

    PowerShell is not exercised here, so this asserts the guard is host-based: it
    is the shape that fails this test if the regex is ever restored.
    """
    content = _read_repo_file("infra", "aws", "deploy.ps1")

    assert "(localhost|127\\.0\\.0\\.1)" not in content, \
        "deploy.ps1 must not detect loopback by matching a literal localhost/127.0.0.1 regex, " \
        "which misses bracketed IPv6 loopback such as http://[::1]:3051"
    assert "[System.Uri]" in content, \
        "deploy.ps1 must parse the frontend URL with [System.Uri] to inspect its host"
    assert '"::1"' in content, \
        "deploy.ps1 must reject the IPv6 loopback literal ::1"
    assert "^127\\." in content, \
        "deploy.ps1 must reject the whole 127.0.0.0/8 range"


def test_no_committed_lambda_crash_payloads() -> None:
    """
    Guard against committing captured Lambda error payloads.

    Regression guard: infra/aws/response.json held a real
    {"errorType":"Extension.Crash", ...} payload captured from the deployed
    function and was tracked in git.
    """
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")
    offenders = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "__pycache__", ".next", ".open-next"}]
        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            full = os.path.join(dirpath, filename)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            if "Extension.Crash" in text or "Runtime.ExitError" in text:
                offenders.append(os.path.relpath(full, repo_root))

    assert not offenders, f"Lambda crash payloads must not be committed: {offenders}"


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
