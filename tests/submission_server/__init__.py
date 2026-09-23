"""Deterministic local HTTP endpoint for submission-boundary tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


@dataclass(frozen=True)
class RecordedRequest:
    path: str
    body: bytes
    content_type: str = ""

    @property
    def is_multipart(self) -> bool:
        return "multipart/form-data" in self.content_type.casefold()

    @property
    def file_parts(self) -> list[str]:
        """Return the field names of every multipart file part."""
        if not self.is_multipart:
            return []
        names: list[str] = []
        for chunk in self.body.split(b"\r\n"):
            if chunk.startswith(b"Content-Disposition:") and b"filename=" in chunk:
                marker = b'name="'
                if marker in chunk:
                    start = chunk.index(marker) + len(marker)
                    names.append(chunk[start : chunk.index(b'"', start)].decode("utf-8", "replace"))
        return names


@dataclass
class _Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


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
            def _respond(self, response: _Response) -> None:
                try:
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.content_type)
                    self.send_header("Content-Length", str(len(response.body)))
                    for key, value in response.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(response.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib protocol name
                if self.path == "/job-with-token":
                    payload = b'<html><head><meta name="csrf-token" content="tok-abc123"></head><body>form</body></html>'
                    self._respond(_Response(200, payload, "text/html; charset=utf-8"))
                    return
                self._respond(_Response(404, b"", "text/plain"))

            def do_POST(self) -> None:  # noqa: N802 - stdlib protocol name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                content_type = self.headers.get("Content-Type", "")
                record = RecordedRequest(self.path, body, content_type)
                owner.requests.append(record)
                count = sum(1 for request in owner.requests if request.path == self.path)

                def json_response(status: int, payload: dict) -> _Response:
                    return _Response(status, json.dumps(payload).encode("utf-8"))

                if self.path == "/submit/timeout":
                    time.sleep(0.2)
                    response = json_response(201, {"status": "submitted"})
                elif self.path == "/submit/success":
                    response = json_response(201, {"status": "submitted"})
                elif self.path == "/job-with-token":
                    body = b'<html><head><meta name="csrf-token" content="tok-abc123"></head><body>form</body></html>'
                    response = _Response(200, body, "text/html; charset=utf-8")
                elif self.path == "/submit/needs-token":
                    # Recusa sem o token que o handshake deve colher em /job-with-token.
                    ok = b"tok-abc123" in body
                    response = json_response(201 if ok else 400, {"status": "submitted" if ok else "bad_request"})
                elif self.path == "/submit/multipart":
                    # A real board rejects an application that carries no resume part.
                    ok = record.is_multipart and any("resume" in name for name in record.file_parts)
                    response = json_response(201 if ok else 422, {"status": "submitted" if ok else "validation_error"})
                elif self.path == "/submit/html":
                    response = _Response(200, b"<html><body><h1>Thank you for applying</h1></body></html>", "text/html; charset=utf-8")
                elif self.path == "/submit/html-unclear":
                    response = _Response(200, b"<html><body><h1>Something happened</h1></body></html>", "text/html; charset=utf-8")
                elif self.path == "/submit/duplicate":
                    response = json_response(201 if count == 1 else 409, {"status": "submitted" if count == 1 else "duplicate"})
                elif self.path == "/submit/validation":
                    response = json_response(422, {"status": "validation_error"})
                elif self.path == "/submit/error":
                    response = json_response(500, {"status": "server_error"})
                elif self.path == "/submit/redirect":
                    response = _Response(303, b"", "text/plain", {"Location": "/confirmation"})
                else:
                    response = json_response(404, {"status": "not_found"})
                self._respond(response)

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
