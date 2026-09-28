"""Tipos neutros compartilhados pelos adapters da V2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ATSInspectionError(ValueError):
    """Snapshot ausente, inconsistente ou de provider não suportado."""


@dataclass(frozen=True)
class NeutralField:
    id: str
    type: str
    label: str
    required: bool
    options: tuple[str, ...] = ()
    value: str = ""


@dataclass(frozen=True)
class NeutralForm:
    provider: str
    page_type: str
    url: str
    title: str
    ready: bool
    fields: tuple[NeutralField, ...]


class ATSAdapter(Protocol):
    provider: str

    def inspect(self, snapshot: Mapping[str, Any]) -> NeutralForm: ...
