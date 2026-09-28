"""Observação explícita de desafios humanos.

Este módulo não resolve desafios e não faz inferência por texto genérico. O
browser deve enviar somente sinais que conseguiu observar na página.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ChallengeState(StrEnum):
    CLEAR = "CLEAR"
    BLOCKING = "BLOCKING"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ChallengeObservation:
    state: ChallengeState
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state.value, "signals": list(self.signals)}


def classify(snapshot: Mapping[str, Any]) -> ChallengeObservation:
    """Valida uma observação do browser sem ampliar seu significado.

    ``state`` é a única fonte de verdade. Sinais desconhecidos ou um estado
    ausente resultam em ``UNKNOWN`` para manter a decisão fail-closed.
    """

    raw_state = snapshot.get("state")
    try:
        state = ChallengeState(raw_state)
    except ValueError:
        return ChallengeObservation(ChallengeState.UNKNOWN)
    raw_signals = snapshot.get("signals", ())
    if not isinstance(raw_signals, (list, tuple)):
        return ChallengeObservation(ChallengeState.UNKNOWN)
    signals = tuple(item for item in raw_signals if isinstance(item, str) and item.strip())
    return ChallengeObservation(state, signals)
