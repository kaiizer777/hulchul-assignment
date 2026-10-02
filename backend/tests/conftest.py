import os
import socket
import threading
import pytest
from fastapi import FastAPI, Request
import uvicorn

s = socket.socket()
s.bind(('127.0.0.1', 0))
port = s.getsockname()[1]
s.close()

os.environ.setdefault("UPSTASH_REDIS_REST_URL", f"http://127.0.0.1:{port}")
os.environ.setdefault("UPSTASH_REDIS_REST_TOKEN", "dummy-mock-token")

app = FastAPI()
storage = {}

@app.post("/")
async def upstash_mock(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = []
    args = body if isinstance(body, list) else []
    result = None
    if args:
        cmd = str(args[0]).upper()
        if cmd == "PING":
            result = "PONG"
        elif cmd == "SET":
            if len(args) >= 3:
                key = str(args[1])
                val = str(args[2])
                storage[key] = val
                result = "OK"
        elif cmd == "GET":
            if len(args) >= 2:
                key = str(args[1])
                result = storage.get(key)
        elif cmd == "DEL":
            if len(args) >= 2:
                key = str(args[1])
                if key in storage:
                    del storage[key]
                    result = 1
                else:
                    result = 0
    return {"result": result}

@pytest.fixture(scope="session", autouse=True)
def mock_upstash_redis_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    yield
