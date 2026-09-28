"""Tipos de domínio da V2.

Os estados pertencem à aplicação inteira, não a um ATS específico. A transição
válida fica centralizada em :mod:`job_agent_v2.application`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    NEW = "NEW"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    FILLING = "FILLING"
    FILLED = "FILLED"
    WAITING_HUMAN = "WAITING_HUMAN"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTING = "SUBMITTING"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    FAILED = "FAILED"
    SUBMITTED = "SUBMITTED"


@dataclass(frozen=True)
class Field:
    """Um campo do formulario real, com o PROMPT separado das OPCOES."""

    key: str
    prompt: str
    options: tuple[str, ...] = ()
    required: bool = False
    kind: str = "text"

    @property
    def identity(self) -> str:
        """Identidade da pergunta = prompt normalizado, nunca o texto de uma opcao."""
        return " ".join(self.prompt.casefold().split())


@dataclass(frozen=True)
class Form:
    fields: tuple[Field, ...] = ()

    def required(self) -> tuple[Field, ...]:
        return tuple(item for item in self.fields if item.required)


@dataclass(frozen=True)
class Resolution:
    answers: dict[str, str] = field(default_factory=dict)
    resolved_from: dict[str, str] = field(default_factory=dict)
    missing: tuple[Field, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class ApplyResult:
    """O que `apply` devolve. Sem contadores de escrita: nao ha escrita aqui."""

    state: State
    reason: str = ""
    missing: tuple[Field, ...] = ()
    answers: dict[str, str] = field(default_factory=dict)
    resolved_from: dict[str, str] = field(default_factory=dict)
    fields: int = 0
    job_url: str = ""
    apply_url: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "job_url": self.job_url,
            "apply_url": self.apply_url,
            "missing": [{"key": f.key, "prompt": f.prompt, "options": list(f.options)} for f in self.missing],
            "answers": dict(self.answers),
            "resolved_from": dict(self.resolved_from),
            "fields": self.fields,
            "uploads": 0,
            "attempts": 0,
            "submission_writes": 0,
        }
