"""Deterministic local HTTP endpoint for submission-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RecordedRequest:
    path: str
    body: bytes


class SubmissionTestServer:
    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        if self._server is None:
            raise RuntimeError("server is not running")
        return f"http://127.0.0.1:{self._server.server_port}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("path must start with /")
        return self.origin + path

    def __enter__(self) -> "SubmissionTestServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib protocol name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.requests.append(RecordedRequest(self.path, body))
                count = sum(1 for request in owner.requests if request.path == self.path)
                if self.path == "/submit/timeout":
                    time.sleep(0.2)
                    status, payload, headers = 201, {"status": "submitted"}, {}
                elif self.path == "/submit/success":
                    status, payload, headers = 201, {"status": "submitted"}, {}
                elif self.path == "/submit/duplicate":
                    status, payload, headers = (201, {"status": "submitted"}, {}) if count == 1 else (409, {"status": "duplicate"}, {})
                elif self.path == "/submit/validation":
                    status, payload, headers = 422, {"status": "validation_error"}, {}
                elif self.path == "/submit/error":
                    status, payload, headers = 500, {"status": "server_error"}, {}
                elif self.path == "/submit/redirect":
                    status, payload, headers = 303, {}, {"Location": "/confirmation"}
                else:
                    status, payload, headers = 404, {"status": "not_found"}, {}
                encoded = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
