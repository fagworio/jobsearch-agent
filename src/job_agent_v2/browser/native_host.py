"""Transporte Native Messaging, sem lógica de candidatura.

Chrome Native Messaging usa um frame com tamanho little-endian seguido de um
objeto JSON UTF-8. Este módulo só lê, valida, despacha e escreve respostas.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import selectors
import socket
import stat
import struct
import sys
from typing import BinaryIO, Any

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


class _FrameDecoder:
    """Decodificador incremental para pipes e sockets não bloqueantes."""

    def __init__(self, *, max_bytes: int = MAX_FRAME_BYTES) -> None:
        self._buffer = bytearray()
        self._max_bytes = max_bytes

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while len(self._buffer) >= _HEADER.size:
            (size,) = _HEADER.unpack(self._buffer[:_HEADER.size])
            if size == 0 or size > self._max_bytes:
                raise NativeMessagingError(f"invalid Native Messaging frame size: {size}")
            end = _HEADER.size + size
            if len(self._buffer) < end:
                break
            frames.append(bytes(self._buffer[_HEADER.size:end]))
            del self._buffer[:end]
        return frames


def default_socket_path() -> Path:
    configured = os.environ.get("JOB_AGENT_V2_NATIVE_SOCKET")
    if configured:
        return Path(configured)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime_dir) if runtime_dir else Path.home() / ".config"
    return root / "job-agent-v2" / "native-messaging.sock"


def _socket_write(connection: socket.socket, payload: bytes) -> None:
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise NativeMessagingError("invalid socket payload size")
    connection.sendall(_HEADER.pack(len(payload)) + payload)


class NativeHostServer:
    """Loop de transporte; o handler é a única dependência de domínio."""

    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        handler: Handler,
        *,
        socket_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.handler = handler
        self.socket_path = Path(socket_path) if socket_path else None

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
        if self.socket_path is not None:
            self._serve_bridge()
            return
        while self.serve_once():
            pass

    def _serve_bridge(self) -> None:
        """Liga o processo Python aberto pelo Chrome ao backend local.

        O Chrome continua sendo o único dono da sessão e do DOM. O socket
        existe apenas no host local para que o backend envie uma mensagem
        tipada à extensão e receba sua resposta correspondente.
        """

        assert self.socket_path is not None
        parent = self.socket_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.stat().st_mode):
                raise NativeMessagingError(f"native socket path is not a socket: {self.socket_path}")
            self.socket_path.unlink()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        listener.listen(1)
        listener.setblocking(False)

        chrome_fd = self.input_stream.fileno()
        os.set_blocking(chrome_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(listener, selectors.EVENT_READ, ("listener", listener))
        selector.register(chrome_fd, selectors.EVENT_READ, ("chrome", chrome_fd))
        backend: socket.socket | None = None
        backend_decoder = _FrameDecoder()
        chrome_decoder = _FrameDecoder()
        pending: set[str] = set()

        try:
            while True:
                for key, _ in selector.select():
                    kind, source = key.data
                    if kind == "listener":
                        connection, _ = source.accept()
                        connection.setblocking(False)
                        if backend is not None:
                            selector.unregister(backend)
                            backend.close()
                            pending.clear()
                        backend = connection
                        selector.register(connection, selectors.EVENT_READ, ("backend", connection))
                        backend_decoder = _FrameDecoder()
                        continue

                    if kind == "backend":
                        assert backend is source
                        data = source.recv(65_536)
                        if not data:
                            selector.unregister(source)
                            source.close()
                            backend = None
                            pending.clear()
                            continue
                        for raw in backend_decoder.feed(data):
                            self._forward_backend_request(raw, source, pending)
                        continue

                    data = os.read(source, 65_536)
                    if not data:
                        return
                    for raw in chrome_decoder.feed(data):
                        self._handle_chrome_frame(raw, backend, pending)
        finally:
            selector.close()
            listener.close()
            if backend is not None:
                backend.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass

    def _forward_backend_request(self, raw: bytes, backend: socket.socket, pending: set[str]) -> None:
        try:
            value: Any = json.loads(raw.decode("utf-8"))
            request = Request.from_object(value)
            validate_request(request)
        except (ProtocolError, SecurityError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _socket_write(backend, Response.failure("invalid-request", str(exc)).to_json())
            return
        pending.add(request.request_id)
        write_frame(self.output_stream, request.to_json())

    def _handle_chrome_frame(
        self,
        raw: bytes,
        backend: socket.socket | None,
        pending: set[str],
    ) -> None:
        try:
            value: Any = json.loads(raw.decode("utf-8"))
            if isinstance(value, dict) and "type" in value:
                request = Request.from_object(value)
                validate_request(request)
                write_response(self.output_stream, self.handler(request))
                return
            response = Response.from_object(value)
        except (ProtocolError, SecurityError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if backend is not None:
                _socket_write(backend, Response.failure("invalid-response", str(exc)).to_json())
            return
        if backend is not None and response.request_id in pending:
            pending.remove(response.request_id)
            _socket_write(backend, response.to_json())


def ping_handler(request: Request) -> Response:
    """Handler mínimo útil para o smoke test do transporte."""
    if request.command is Command.HELLO:
        return Response.success(request.request_id, {"name": "job-agent-v2", "protocol_version": 1})
    if request.command is Command.PING:
        return Response.success(request.request_id, {"type": "PONG"})
    return Response.failure(request.request_id, "command is not handled by the transport smoke handler")


def main() -> int:
    """Entry point do host Native Messaging instalado pelo usuário."""
    bridge = os.environ.get("JOB_AGENT_V2_NATIVE_MODE") == "bridge"
    socket_path = default_socket_path() if bridge else None
    NativeHostServer(sys.stdin.buffer, sys.stdout.buffer, ping_handler, socket_path=socket_path).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
