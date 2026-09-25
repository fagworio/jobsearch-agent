"""Modelos imutáveis do pacote `challenge_resolution`.

Todos os dataclasses são `frozen=True` e `slots=True`. Nenhum deles contém
tokens, cookies, segredos ou respostas de challenge. A ausência desses campos é
verificada no import (ver `provenance._assert_no_sensitive_fields`).

Regra de ouro: se um campo não pode aparecer no journal de produção, ele não
pode aparecer aqui.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .provenance import ChallengeProvenance, _assert_no_sensitive_fields
from .types import (
    CapabilityStatus,
    ChallengeObservation,
    ChallengeProvider,
    ResolutionStatus,
    ValidationStatus,
)


# ---------------------------------------------------------------------------
# Payload — dados NÃO sensíveis passados ao executor
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResolutionPayload:
    """Instrução opaca para o executor de interação.

    `kind` identifica a natureza da interação (ex.: `"handoff"`, `"click"`).
    `data` é um mapping de valores NÃO sensíveis. Tokens jamais entram aqui:
    quem os possui é o executor, e apenas durante o escopo da chamada.
    """

    kind: str
    data: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Rejeita campos proibidos logo na construção, não em runtime.
        forbidden = {"token", "cookie", "secret", "answer", "response"}
        leaked = forbidden.intersection(self.data.keys())
        if leaked:
            raise ValueError(
                f"ResolutionPayload não pode conter campos sensíveis: {sorted(leaked)}"
            )


# ---------------------------------------------------------------------------
# Resultado de uma única tentativa de resolução
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Resultado de uma tentativa de resolução de um único round.

    Produzido pelo `ChallengeResolutionEngine` (ou por uma `ResolutionStrategy`)
    e consumido pelo `ChallengeResolutionOrchestrator`.
    """

    status: ResolutionStatus
    provider: ChallengeProvider
    challenge_type: str
    session_id: str
    strategy_name: str | None = None
    duration_seconds: float = 0.0
    error_code: str | None = None
    error_message: str | None = None

    @property
    def is_terminal(self) -> bool:
        """True se o resultado não deve disparar novo round."""
        return self.status in {
            ResolutionStatus.RESOLVED,
            ResolutionStatus.FAILED,
            ResolutionStatus.UNSUPPORTED,
            ResolutionStatus.HUMAN_REQUIRED,
        }

    @property
    def is_retryable(self) -> bool:
        """True se o orquestrador deve tentar outro round."""
        return self.status in {
            ResolutionStatus.TEMPORARILY_UNAVAILABLE,
        }


# ---------------------------------------------------------------------------
# Resultado da validação pós-tentativa
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Veredito do validador após uma tentativa.

    Sempre carrega a `ChallengeObservation` que fundamentou a decisão, para que
    o journal registre o "porquê" sem reobservar.
    """

    status: ValidationStatus
    observation: ChallengeObservation

    @property
    def accepted(self) -> bool:
        return self.status == ValidationStatus.ACCEPTED

    @property
    def repeated(self) -> bool:
        return self.status == ValidationStatus.REPEATED

    @property
    def provider_rejected(self) -> bool:
        return self.status == ValidationStatus.PROVIDER_REJECTED


# ---------------------------------------------------------------------------
# Limites da orquestração — explícitos, imutáveis, auditáveis
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrchestratorLimits:
    """Limites duros do loop de orquestração.

    Valores default conservadores. Cada limite é verificado independentemente
    pelo orquestrador — nenhum deles é decorativo.
    """

    max_rounds: int = 3
    timeout_seconds: float = 120.0
    max_duration_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds deve ser >= 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds deve ser > 0")
        if self.max_duration_seconds < self.timeout_seconds:
            raise ValueError("max_duration_seconds deve ser >= timeout_seconds")


# ---------------------------------------------------------------------------
# Resultado agregado da orquestração — entregue ao ApplicationLoop
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OrchestratorOutcome:
    """Saída do `ChallengeResolutionOrchestrator`.

    É o único objeto que o `ApplicationLoop` consome: o resultado lógico
    (`resolved`) e a proveniência auditável.
    """

    resolved: bool
    rounds: int
    duration_seconds: float
    final_status: ResolutionStatus
    provenance: ChallengeProvenance

    def as_journal(self) -> Mapping[str, object]:
        """Projeção segura para o journal.

        NÃO inclui nada da `provenance` que não esteja explicitamente listado.
        """
        return {
            "resolved": self.resolved,
            "rounds": self.rounds,
            "duration_seconds": round(self.duration_seconds, 3),
            "final_status": self.final_status.value,
            "session_id": self.provenance.session_id,
            "provider": self.provenance.provider,
            "challenge_type": self.provenance.challenge_type,
        }


# ---------------------------------------------------------------------------
# Resultado de capability lookup — evita strings mágicas
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CapabilityLookup:
    """Resultado de consultar a `CapabilityMatrix`.

    Nunca é None: providers desconhecidos caem em `CapabilityStatus.UNSUPPORTED`.
    """

    status: CapabilityStatus
    provider: str
    challenge_type: str
    reason: str | None = None


for _dataclass in (
    ResolutionPayload,
    ResolutionResult,
    ValidationResult,
    OrchestratorLimits,
    OrchestratorOutcome,
    CapabilityLookup,
):
    _assert_no_sensitive_fields(_dataclass)
