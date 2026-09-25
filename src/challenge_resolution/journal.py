"""Journal do subsistema de resolucao.

O Protocol e minimalista de proposito — `emit(kind, /, **fields)` — para que o
journal real do `jobsearch-agent` (`observability.append_event`) seja usado sem
adapter: ele ja tem essa forma.

Duas regras que valem para QUALQUER implementacao:

1. `emit` nao levanta para o chamador. Um journal que quebra nao pode derrubar
   uma candidatura.
2. `fields` e material de AUDITORIA: JSON-serializavel e sem segredo. Nada de
   objeto de runtime, token, cookie ou resposta de desafio.

A unica excecao a (1) e deliberada e vive no journal de TESTE: `InMemoryJournal`
recusa um `kind` fora de `ALL_EVENT_KINDS` para que um typo apareca no teste em
vez de virar um evento invisivel em producao.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable


@runtime_checkable
class Journal(Protocol):
    """Superficie minima de journal."""

    def emit(self, kind: str, /, **fields: object) -> None: ...


@dataclass(frozen=True, slots=True)
class CapturedEvent:
    kind: str
    fields: Mapping[str, object]


@dataclass
class InMemoryJournal:
    """Journal de teste: guarda tudo e recusa `kind` desconhecido."""

    events: list[CapturedEvent] = field(default_factory=list)
    strict_kinds: bool = True

    def emit(self, kind: str, /, **fields: object) -> None:
        if self.strict_kinds:
            from .events import ALL_EVENT_KINDS

            if kind not in ALL_EVENT_KINDS:
                raise ValueError(f"evento desconhecido: {kind}")
        self.events.append(CapturedEvent(kind=kind, fields=dict(fields)))

    # -- helpers de assercao ------------------------------------------------

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def find(self, kind: str) -> list[CapturedEvent]:
        return [event for event in self.events if event.kind == kind]

    def last(self, kind: str) -> CapturedEvent | None:
        matches = self.find(kind)
        return matches[-1] if matches else None


class NullJournal:
    """Journal que descarta tudo (producao, quando journal e opcional)."""

    def emit(self, kind: str, /, **fields: object) -> None:
        return None


__all__ = ["CapturedEvent", "InMemoryJournal", "Journal", "NullJournal"]
