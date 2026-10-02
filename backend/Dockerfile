# The Lambda Web Adapter ships precompiled binaries in ECR Public, not as GitHub
# release assets. The v0.8.4 GitHub release publishes zero assets, so any
# releases/download URL for it returns 404 and silently produces a text file at
# /opt/extensions/lambda-adapter, which Lambda then reports as
# "Extension.Crash ... exit status 127".
FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 AS lambda-adapter

FROM python:3.12-slim

COPY --from=lambda-adapter /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

# Set environment variables for Lambda Web Adapter and FastAPI.
# Lambda Web Adapter treats PORT as its traffic port, so uvicorn and the
# adapter must agree on 8051.
ENV PORT=8051
ENV AWS_LWA_ENABLE_COMPRESSION=false
ENV AWS_LWA_INVOKE_MODE=response_stream

# Install Python dependencies. Built from the repository root so that the
# `backend` package layout that backend/main.py imports from is preserved.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into /app/backend so that `from backend.config import
# settings` (and the other absolute backend.* imports) resolve at runtime.
COPY backend/ ./backend/

EXPOSE 8051

# Start Uvicorn ASGI server using python -m
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8051"]