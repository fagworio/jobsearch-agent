"""Contexto de uma sessão de challenge.

Uma `ChallengeSession` nasce de uma `ChallengeObservation` e vive até o final da
orquestração. Ela NÃO carrega browser, page ou qualquer objeto de runtime — isso
é responsabilidade do executor, que recebe a sessão como metadado.

A separação é deliberada: a sessão é serializável; o runtime do browser não é.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .provenance import _assert_no_sensitive_fields
from .types import (
    ChallengeObservation,
    ChallengePhase,
    ChallengeProvider,
    enum_value,
)


@dataclass(frozen=True, slots=True)
class ChallengeSession:
    """Identidade e contexto de uma sessão de challenge.

    Imutável. Pode ser serializada para o journal. Nunca contém referências a
    objetos vivos (page, browser, context).
    """

    session_id: str
    application_id: str
    provider: ChallengeProvider
    challenge_type: str
    phase: ChallengePhase
    started_at: datetime
    initial_confidence: float

    @classmethod
    def from_observation(
        cls,
        observation: ChallengeObservation,
        *,
        application_id: str,
    ) -> "ChallengeSession":
        """Constrói uma sessão a partir de uma observação do `challenge-guard`.

        O `session_id` combina application + provider + tipo + instante, o que
        dá unicidade sem estado global e sem depender do `session_id` do guard —
        que é escopo do monitor, não do domínio da candidatura.
        """
        started_at = datetime.now(timezone.utc)
        challenge_type = enum_value(observation.challenge_type)
        session_id = (
            f"{application_id}:{observation.provider.value}:"
            f"{challenge_type}:{int(started_at.timestamp() * 1000)}"
        )
        return cls(
            session_id=session_id,
            application_id=application_id,
            provider=observation.provider,
            challenge_type=challenge_type,
            phase=observation.phase,
            started_at=started_at,
            initial_confidence=float(getattr(observation, "confidence", 0.0)),
        )

    def as_journal(self) -> Mapping[str, Any]:
        return {
            "session_id": self.session_id,
            "application_id": self.application_id,
            "provider": self.provider.value,
            "challenge_type": self.challenge_type,
            "phase": self.phase.value,
            "started_at": self.started_at.isoformat(),
            "initial_confidence": self.initial_confidence,
        }


_assert_no_sensitive_fields(ChallengeSession)
