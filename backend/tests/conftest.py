import asyncio
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytest
import os

class MockUpstashHandler(BaseHTTPRequestHandler):
    storage = {}

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            args = json.loads(body.decode('utf-8'))
        except Exception:
            args = []

        result = None
        if args:
            cmd = args[0].upper()
            if cmd == "PING":
                result = "PONG"
            elif cmd == "SET":
                if len(args) >= 3:
                    key = args[1]
                    val = args[2]
                    self.storage[key] = val
                    result = "OK"
            elif cmd == "GET":
                if len(args) >= 2:
                    key = args[1]
                    result = self.storage.get(key)
            elif cmd == "DEL":
                if len(args) >= 2:
                    key = args[1]
                    if key in self.storage:
                        del self.storage[key]
                        result = 1
                    else:
                        result = 0

        response_data = {"result": result}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response_data).encode('utf-8'))

    def log_message(self, format, *args):
        pass

@pytest.fixture(scope="session", autouse=True)
def mock_upstash_redis_server():
    server = HTTPServer(('127.0.0.1', 0), MockUpstashHandler)
    port = server.server_port
    os.environ["UPSTASH_REDIS_REST_URL"] = f"http://127.0.0.1:{port}"
    os.environ["UPSTASH_REDIS_REST_TOKEN"] = "dummy-mock-token"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield
    server.shutdown()
    server.server_close()
