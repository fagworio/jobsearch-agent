"""Validações de segurança do contrato Python ↔ extensão."""

from __future__ import annotations

from collections.abc import Mapping

from .protocol import Request


FORBIDDEN_KEYS = frozenset({"eval_js", "shell", "execute_command", "python", "arbitrary_script"})


class SecurityError(ValueError):
    """Mensagem tenta ampliar o contrato fechado do browser."""


def _contains_forbidden(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if name in FORBIDDEN_KEYS:
                return name
            found = _contains_forbidden(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _contains_forbidden(item)
            if found:
                return found
    return None


def validate_request(request: Request) -> None:
    forbidden = _contains_forbidden(request.payload)
    if forbidden:
        raise SecurityError(f"forbidden browser payload key: {forbidden}")
