"""Contrato fechado de mensagens entre Python e a extensão Chrome.

O protocolo é deliberadamente pequeno: a extensão só recebe operações de
browser declaradas neste enum. Não existe uma mensagem genérica para executar
JavaScript, shell ou Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Mapping


PROTOCOL_VERSION = 1


class Command(StrEnum):
    HELLO = "HELLO"
    PING = "PING"
    GET_PAGE = "GET_PAGE"
    INSPECT_FORM = "INSPECT_FORM"
    FILL_FORM = "FILL_FORM"
    READ_FORM = "READ_FORM"
    UPLOAD_ARTIFACT = "UPLOAD_ARTIFACT"
    GET_CHALLENGE_STATE = "GET_CHALLENGE_STATE"
    REQUEST_SUBMIT = "REQUEST_SUBMIT"
    GET_SUBMIT_RESULT = "GET_SUBMIT_RESULT"


COMMANDS = frozenset(command.value for command in Command)
_MAX_JSON_BYTES = 4 * 1024 * 1024


class ProtocolError(ValueError):
    """Mensagem ausente, inválida ou fora do contrato fechado."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{name} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Request:
    version: int
    request_id: str
    command: Command
    payload: dict[str, Any]

    @classmethod
    def create(cls, request_id: str, command: Command, payload: Mapping[str, Any] | None = None) -> "Request":
        if not isinstance(request_id, str) or not request_id.strip():
            raise ProtocolError("request_id must be a non-empty string")
        return cls(PROTOCOL_VERSION, request_id, command, dict(payload or {}))

    @classmethod
    def from_object(cls, value: object) -> "Request":
        data = _object(value, "request")
        version = data.get("version")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {version!r}")
        request_id = _required_string(data, "request_id")
        raw_type = _required_string(data, "type")
        try:
            command = Command(raw_type)
        except ValueError as exc:
            raise ProtocolError(f"unsupported command: {raw_type}") from exc
        payload = _object(data.get("payload", {}), "payload")
        return cls(version, request_id, command, payload)

    def to_object(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "request_id": self.request_id,
            "type": self.command.value,
            "payload": dict(self.payload),
        }

    def to_json(self) -> bytes:
        encoded = json.dumps(self.to_object(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_JSON_BYTES:
            raise ProtocolError("message exceeds maximum size")
        return encoded


@dataclass(frozen=True)
class Response:
    request_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str = ""
    version: int = PROTOCOL_VERSION

    @classmethod
    def success(cls, request_id: str, result: Mapping[str, Any] | None = None) -> "Response":
        return cls(request_id=request_id, ok=True, result=dict(result or {}))

    @classmethod
    def failure(cls, request_id: str, error: str) -> "Response":
        if not error.strip():
            raise ProtocolError("error must be a non-empty string")
        return cls(request_id=request_id, ok=False, error=error)

    @classmethod
    def from_object(cls, value: object) -> "Response":
        data = _object(value, "response")
        if data.get("version") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version: {data.get('version')!r}")
        request_id = _required_string(data, "request_id")
        ok = data.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError("ok must be boolean")
        result = _object(data.get("result", {}), "result") if ok else None
        error = str(data.get("error", "")) if not ok else ""
        if not ok and not error.strip():
            raise ProtocolError("failed response requires error")
        return cls(request_id=request_id, ok=ok, result=result, error=error)

    def to_object(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": self.version,
            "request_id": self.request_id,
            "ok": self.ok,
        }
        if self.ok:
            value["result"] = dict(self.result or {})
        else:
            value["error"] = self.error
        return value

    def to_json(self) -> bytes:
        encoded = json.dumps(self.to_object(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_JSON_BYTES:
            raise ProtocolError("message exceeds maximum size")
        return encoded


def decode_json(raw: bytes) -> Request | Response:
    if not raw or len(raw) > _MAX_JSON_BYTES:
        raise ProtocolError("invalid or oversized message")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("message is not valid UTF-8 JSON") from exc
    if isinstance(value, Mapping) and "type" in value:
        return Request.from_object(value)
    return Response.from_object(value)
