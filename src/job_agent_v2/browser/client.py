"""Clientes controlados da V2.

``NativeMessagingClient`` só transporta mensagens do contrato fechado. O
loader Playwright continua disponível exclusivamente para testes controlados e
medição, não como runtime de produção da candidatura.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import socket
import subprocess
from typing import Any, BinaryIO
from uuid import uuid4

from .native_host import default_socket_path, read_frame, write_frame
from .protocol import Command, Request, Response


class NativeMessagingClient:
    """Cliente de transporte para um host Native Messaging local."""

    def __init__(
        self,
        command: Sequence[str] | None = None,
        *,
        socket_path: str | Path | None = None,
        connect_timeout: float = 10.0,
        request_timeout: float = 60.0,
    ) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._request_timeout = request_timeout
        if command is not None:
            self._process = subprocess.Popen(
                tuple(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        else:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(connect_timeout)
            self._socket.connect(str(socket_path or default_socket_path()))
            self._socket.settimeout(request_timeout)

    def __enter__(self) -> "NativeMessagingClient":
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()

    @property
    def _stdin(self) -> BinaryIO:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("native host stdin is unavailable")
        return self._process.stdin

    @property
    def _stdout(self) -> BinaryIO:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("native host stdout is unavailable")
        return self._process.stdout

    def request(self, request: Request) -> Response:
        if self._socket is not None:
            payload = request.to_json()
            self._socket.sendall(len(payload).to_bytes(4, "little") + payload)
            header = self._read_socket_exact(4)
            size = int.from_bytes(header, "little")
            if size <= 0 or size > 4 * 1024 * 1024:
                raise RuntimeError(f"invalid native socket frame size: {size}")
            raw = self._read_socket_exact(size)
            reply = Response.from_object(json.loads(raw.decode("utf-8")))
            if reply.request_id != request.request_id:
                raise RuntimeError("native socket response request_id does not match")
            return reply
        write_frame(self._stdin, request.to_json())
        raw = read_frame(self._stdout)
        if raw is None:
            raise RuntimeError("native host closed before responding")
        reply = Response.from_object(json.loads(raw.decode("utf-8")))
        if reply.request_id != request.request_id:
            raise RuntimeError("native host response request_id does not match")
        return reply

    def call(self, command: Command, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self.request(Request.create(uuid4().hex, command, payload))
        if not reply.ok:
            raise RuntimeError(reply.error)
        return dict(reply.result or {})

    def ping(self) -> Response:
        return self.request(Request.create("client-ping", Command.PING))

    def auth_state(self, *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.GET_AUTH_STATE, self._tab_payload(tab_id=tab_id))

    def get_page(self) -> dict[str, Any]:
        return self.call(Command.GET_PAGE)

    def inspect_discovery_results(self) -> dict[str, Any]:
        return self.call(Command.GET_DISCOVERY_RESULTS)

    def inspect_discovery_filters(self) -> dict[str, Any]:
        return self.call(Command.GET_DISCOVERY_FILTERS)

    def discover_query(self, query: str, work_type: list[str] | None = None) -> dict[str, Any]:
        return self.call(Command.DISCOVER_QUERY, {"query": query, "work_type": list(work_type or ["remote"])})

    def open_job(self, job_id: str, url: str, provider: str = "greenhouse") -> dict[str, Any]:
        return self.call(Command.OPEN_JOB, {"job_id": job_id, "url": url, "provider": provider})

    def inspect_job_details(self, *, tab_id: int, job_id: str, provider: str = "greenhouse") -> dict[str, Any]:
        return self.call(Command.GET_JOB_DETAILS, {
            "tab_id": tab_id,
            "job_id": job_id,
            "provider": provider,
        })

    def get_tab_context(self, tab_id: int) -> dict[str, Any]:
        return self.call(Command.GET_TAB_CONTEXT, {"tab_id": tab_id})

    def wait_for_application(self, tab_id: int, job_id: str, provider: str = "greenhouse") -> dict[str, Any]:
        return self.call(Command.WAIT_FOR_APPLICATION, {
            "tab_id": tab_id,
            "job_id": job_id,
            "provider": provider,
        })

    def remove_repeatable_entry(self, entry_type: str, index: int, *, tab_id: int) -> dict[str, Any]:
        return self.call(Command.REMOVE_REPEATABLE_ENTRY, {
            "entry_type": entry_type,
            "index": index,
            "tab_id": tab_id,
        })

    @staticmethod
    def _tab_payload(payload: dict[str, Any] | None = None, tab_id: int | None = None) -> dict[str, Any]:
        value = dict(payload or {})
        if tab_id is not None:
            value["tab_id"] = tab_id
        return value

    def inspect_form(self, *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.INSPECT_FORM, self._tab_payload(tab_id=tab_id))

    def inspect_field_options(self, field_id: str, *, tab_id: int | None = None) -> dict[str, Any]:
        # A provider widget can fail to answer while its lazy menu is being
        # mounted. Do not let that block the whole campaign: this read is
        # optional and the submit boundary has not been crossed yet.
        if self._socket is None:
            return self.call(Command.GET_FIELD_OPTIONS, self._tab_payload({"field_id": field_id}, tab_id))
        previous_timeout = self._socket.gettimeout()
        self._socket.settimeout(min(previous_timeout or 60.0, 15.0))
        try:
            return self.call(Command.GET_FIELD_OPTIONS, self._tab_payload({"field_id": field_id}, tab_id))
        except socket.timeout as exc:
            raise TimeoutError(f"field option inspection timed out: {field_id}") from exc
        finally:
            self._socket.settimeout(previous_timeout)

    def fill_form(self, payload: dict[str, Any], *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.FILL_FORM, self._tab_payload(payload, tab_id))

    def read_form(self, *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.READ_FORM, self._tab_payload(tab_id=tab_id))

    def upload_artifact(self, payload: dict[str, Any], *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.UPLOAD_ARTIFACT, self._tab_payload(payload, tab_id))

    def challenge_state(self, *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.GET_CHALLENGE_STATE, self._tab_payload(tab_id=tab_id))

    def request_submit(self, payload: dict[str, Any], *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.REQUEST_SUBMIT, self._tab_payload(payload, tab_id))

    def submit_result(self, *, tab_id: int | None = None) -> dict[str, Any]:
        return self.call(Command.GET_SUBMIT_RESULT, self._tab_payload(tab_id=tab_id))

    def _read_socket_exact(self, size: int) -> bytes:
        if self._socket is None:
            raise RuntimeError("native socket is unavailable")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._socket.recv(remaining)
            if not chunk:
                raise RuntimeError("native host closed the backend socket")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
            self._process.terminate()
            self._process.wait(timeout=5)
            self._process = None


def open_page_html(url: str, *, timeout_ms: float = 45_000) -> str:
    raise RuntimeError(
        "open_page_html is test-only; use NativeMessagingClient against the persistent Chrome profile"
    )
