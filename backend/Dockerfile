FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 AS lambda-adapter
FROM python:3.12-slim

# Copy AWS Lambda Web Adapter extension binary
COPY --from=lambda-adapter /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app/backend

# Set environment variables for Lambda Web Adapter and FastAPI
ENV PORT=8051
ENV AWS_LWA_ENABLE_COMPRESSION=false
ENV AWS_LWA_INVOKE_MODE=response_stream

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into backend package directory
COPY . .

WORKDIR /app

EXPOSE 8051

# Start Uvicorn ASGI server
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8051"]
