"""Perfil explícito do usuário; não gera fatos pessoais."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def normalize_profile(values: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in values.items()}


def load_profile(path: str | Path) -> dict[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile must be a JSON object")
    return normalize_profile(payload)
