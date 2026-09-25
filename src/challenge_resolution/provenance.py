"""Proveniência de uma sessão de challenge.

Este dataclass é a ÚNICA representação persistível de uma interação de
challenge. Ele é deliberadamente restrito: nada de tokens, cookies, respostas
ou qualquer material que possa ser reutilizado para resolver outro desafio.

A ausência de campos sensíveis não é promessa de revisão: `_assert_no_sensitive_fields`
roda no **import** do módulo. Um campo novo com nome proibido derruba o pacote
antes de qualquer teste — e antes de qualquer journal em produção.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

#: Partes de nome de campo que NUNCA podem existir aqui. Um campo cujo nome
#: contenha qualquer uma delas quebra o import do pacote.
FORBIDDEN_FIELD_PARTS: tuple[str, ...] = (
    "token",
    "cookie",
    "secret",
    "answer",
    "response",
    "payload",
    "headers",
    "credentials",
    "jwt",
    "bearer",
    "session_key",
    "password",
    "api_key",
)


def _assert_no_sensitive_fields(cls: type) -> None:
    """Falha no import se algum campo tiver nome de material sensível."""
    for field in dataclasses.fields(cls):
        lowered = field.name.casefold()
        for part in FORBIDDEN_FIELD_PARTS:
            if part in lowered:
                raise TypeError(
                    f"{cls.__name__}.{field.name} parece material sensível "
                    f"({part!r}): proveniência não pode carregar segredo"
                )


@dataclass(frozen=True, slots=True)
class ChallengeProvenance:
    """Registro auditável de uma sessão de challenge.

    Imutável. Construída ao final da orquestração e emitida ao journal.
    """

    session_id: str
    application_id: str
    provider: str
    challenge_type: str
    rounds: int
    started_at: datetime
    finished_at: datetime
    final_status: str          # ResolutionStatus.value
    max_confidence: float
    strategy_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at não pode ser anterior a started_at")
        if not (0.0 <= self.max_confidence <= 1.0):
            raise ValueError("max_confidence deve estar em [0.0, 1.0]")
        if self.rounds < 0:
            raise ValueError("rounds não pode ser negativo")

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def as_journal(self) -> dict[str, object]:
        """Projeção segura para o journal.

        Não adicionar campos aqui sem revisar a política de não-persistência de
        segredos: esta é a forma que circula em log, terminal e pacote de
        handoff.
        """
        return {
            "session_id": self.session_id,
            "application_id": self.application_id,
            "provider": self.provider,
            "challenge_type": self.challenge_type,
            "rounds": self.rounds,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "final_status": self.final_status,
            "max_confidence": self.max_confidence,
            "strategy_names": list(self.strategy_names),
        }


_assert_no_sensitive_fields(ChallengeProvenance)
