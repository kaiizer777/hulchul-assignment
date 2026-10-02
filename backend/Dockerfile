FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/extensions && \
    curl -Lo /opt/extensions/lambda-adapter https://github.com/awslabs/aws-lambda-adapter/releases/download/v0.8.4/lambda-adapter-v0.8.4-linux-amd64 && \
    chmod +x /opt/extensions/lambda-adapter

WORKDIR /app

# Set environment variables for Lambda Web Adapter and FastAPI
ENV PORT=8051
ENV AWS_LWA_ENABLE_COMPRESSION=false
ENV AWS_LWA_INVOKE_MODE=response_stream

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8051

# Start Uvicorn ASGI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8051"]
