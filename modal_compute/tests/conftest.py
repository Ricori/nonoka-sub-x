"""Shared fixtures, and the engine on the path the way the images put it there."""

from __future__ import annotations

import json
import sys
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


ROOT = Path(__file__).resolve().parents[2]
for source in (
    ROOT / "modal_compute",
    ROOT / "third_party" / "finesub" / "src",
    ROOT / "src",
):
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))


class FakeWorker:
    """The Worker's internal API, scripted per endpoint.

    `script(endpoint, status, body)` queues one response for the next call to
    `endpoint` (the last path segment: `run`, `events`, `source`, ...); an
    endpoint with nothing queued answers 200 `{"active": true}`. Bodies that
    are `bytes` are sent raw. Every request is recorded in `requests`.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._queues: dict[str, deque] = defaultdict(deque)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401 - silence
                pass

            def _serve(self) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                endpoint = parsed.path.rstrip("/").rsplit("/", 1)[-1]
                fake.requests.append({
                    "method": self.command,
                    "path": parsed.path,
                    "endpoint": endpoint,
                    "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                    "headers": dict(self.headers),
                    "json": json.loads(raw) if raw else None,
                })
                queue = fake._queues[endpoint]
                status, body, headers = queue.popleft() if queue else (200, {"active": True}, {})
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _serve
            do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def script(self, endpoint: str, status: int = 200, body: object = None, headers: dict | None = None) -> None:
        self._queues[endpoint].append((status, {"active": True} if body is None else body, headers or {}))

    def calls(self, endpoint: str) -> list[dict]:
        return [request for request in self.requests if request["endpoint"] == endpoint]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def worker():
    fake = FakeWorker()
    yield fake
    fake.close()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()
