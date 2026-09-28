"""Transporte Native Messaging, sem lógica de candidatura.

Chrome Native Messaging usa um frame com tamanho little-endian seguido de um
objeto JSON UTF-8. Este módulo só lê, valida, despacha e escreve respostas.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import struct
import sys
from typing import BinaryIO

from .protocol import Command, ProtocolError, Request, Response
from .security import SecurityError, validate_request


_HEADER = struct.Struct("<I")
MAX_FRAME_BYTES = 4 * 1024 * 1024
Handler = Callable[[Request], Response]


class NativeMessagingError(RuntimeError):
    """Erro de framing ou de transporte Native Messaging."""


def read_frame(stream: BinaryIO, *, max_bytes: int = MAX_FRAME_BYTES) -> bytes | None:
    header = stream.read(_HEADER.size)
    if header == b"":
        return None
    if len(header) != _HEADER.size:
        raise NativeMessagingError("truncated Native Messaging header")
    (size,) = _HEADER.unpack(header)
    if size == 0 or size > max_bytes:
        raise NativeMessagingError(f"invalid Native Messaging frame size: {size}")
    body = stream.read(size)
    if len(body) != size:
        raise NativeMessagingError("truncated Native Messaging frame")
    return body


def write_frame(stream: BinaryIO, payload: bytes, *, max_bytes: int = MAX_FRAME_BYTES) -> None:
    if not payload or len(payload) > max_bytes:
        raise NativeMessagingError("invalid Native Messaging payload size")
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def write_response(stream: BinaryIO, response: Response) -> None:
    write_frame(stream, response.to_json())


class NativeHostServer:
    """Loop de transporte; o handler é a única dependência de domínio."""

    def __init__(self, input_stream: BinaryIO, output_stream: BinaryIO, handler: Handler) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.handler = handler

    def serve_once(self) -> bool:
        raw = read_frame(self.input_stream)
        if raw is None:
            return False
        try:
            request = json.loads(raw.decode("utf-8"))
            parsed = Request.from_object(request)
            validate_request(parsed)
            response = self.handler(parsed)
            if response.request_id != parsed.request_id:
                raise ProtocolError("handler returned a different request_id")
        except (ProtocolError, SecurityError, NativeMessagingError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            request_id = "invalid-request"
            try:
                if isinstance(request, dict) and isinstance(request.get("request_id"), str):
                    request_id = request["request_id"]
            except UnboundLocalError:
                pass
            response = Response.failure(request_id, str(exc))
        write_response(self.output_stream, response)
        return True

    def serve_forever(self) -> None:
        while self.serve_once():
            pass


def ping_handler(request: Request) -> Response:
    """Handler mínimo útil para o smoke test do transporte."""
    if request.command is Command.HELLO:
        return Response.success(request.request_id, {"name": "job-agent-v2", "protocol_version": 1})
    if request.command is Command.PING:
        return Response.success(request.request_id, {"type": "PONG"})
    return Response.failure(request.request_id, "command is not handled by the transport smoke handler")


def main() -> int:
    """Entry point do host Native Messaging instalado pelo usuário."""
    NativeHostServer(sys.stdin.buffer, sys.stdout.buffer, ping_handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
