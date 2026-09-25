"""Tipos primitivos do pacote `challenge_resolution`.

Este módulo não importa nada além da stdlib e do `challenge_guard` — e apenas
os enums/modelos de observação dele. Nada aqui carrega estado ou efeito
colateral.

Diferenças deliberadas em relação ao rascunho da Fase 0 (todas por
incompatibilidade com o `challenge-guard` v0.1.0 fixado por SHA):

1. O guard **não** tem `challenge_guard.observations`: `ChallengeObservation`
   vive em `challenge_guard.models`.
2. `CapabilityStatus` não existia no rascunho, mas é usado pela matriz e pelo
   Engine — foi definido aqui.
3. `str(membro_do_enum)` devolve `"ChallengeType.CHECKBOX"` (os enums do guard
   derivam de `str` mas não são `StrEnum`). Todo o pacote usa `enum_value()`
   para obter `"checkbox"`. Gravar o nome da classe no journal seria ruído.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, Literal, TypeAlias

# ---------------------------------------------------------------------------
# Reexports controlados do challenge_guard
# ---------------------------------------------------------------------------
# Reexportamos explicitamente para que consumidores deste pacote não precisem
# importar `challenge_guard` diretamente. Isso também permite trocar a
# implementação do guard sem quebrar consumidores, desde que os enums
# permaneçam compatíveis.
from challenge_guard.models import (
    ChallengeDecisionStatus,
    ChallengeObservation,
    ChallengePhase,
    ChallengeProvider,
    ChallengeType,
)

__all__ = [
    "CapabilityStatus",
    "ResolutionStatus",
    "ValidationStatus",
    "OrchestratorState",
    "ProviderKey",
    "ChallengeTypeKey",
    "CapabilityKey",
    "ResolutionOutcome",
    "ChallengeProvider",
    "ChallengePhase",
    "ChallengeType",
    "ChallengeDecisionStatus",
    "ChallengeObservation",
    "UNKNOWN_PROVIDER",
    "UNKNOWN_CHALLENGE_TYPE",
    "enum_value",
]


def enum_value(value: object) -> str:
    """Valor canônico de um membro de enum do guard, ou o texto como está.

    `str(ChallengeType.CHECKBOX)` devolve `"ChallengeType.CHECKBOX"`; o que o
    journal e a matriz de capability querem é `"checkbox"`.
    """
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    return str(value)


# ---------------------------------------------------------------------------
# Status de resolução — usado pelo Engine e pelo Orquestrador
# ---------------------------------------------------------------------------

class ResolutionStatus(str, Enum):
    """Resultado terminal de uma tentativa de resolução de um único round.

    - RESOLVED:                o provider aceitou a interação.
    - FAILED:                  o provider recusou ou a estratégia falhou.
    - EXPIRED:                 o tempo esgotou antes de qualquer conclusão.
    - UNSUPPORTED:             não há estratégia para este provider/tipo.
    - HUMAN_REQUIRED:          a resolução foi delegada a um humano.
    - TEMPORARILY_UNAVAILABLE: o provider está indisponível (retryable).
    """

    RESOLVED = "resolved"
    FAILED = "failed"
    EXPIRED = "expired"
    UNSUPPORTED = "unsupported"
    HUMAN_REQUIRED = "human_required"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"


# ---------------------------------------------------------------------------
# Capability — política declarada, nunca inferida em runtime
# ---------------------------------------------------------------------------

class CapabilityStatus(str, Enum):
    """O que o produto declara saber fazer com um par (provider, tipo).

    SUPPORTED               existe estratégia e ela pode interagir;
    HUMAN_REQUIRED          a resolução é de uma pessoa (handoff);
    TEMPORARILY_UNAVAILABLE o provider está fora do ar (retryable);
    UNSUPPORTED             não há caminho — e é o default fail-safe.
    """

    SUPPORTED = "supported"
    HUMAN_REQUIRED = "human_required"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------
# Status de validação — resultado de reobservar após uma tentativa
# ---------------------------------------------------------------------------

class ValidationStatus(str, Enum):
    """Veredito do validador após uma tentativa de resolução.

    Derivado exclusivamente do `ChallengeDecision` produzido pelo
    `ChallengeMonitor`. Não introduz estados novos na máquina do guard.
    """

    ACCEPTED = "accepted"                    # o provider marcou sucesso
    REPEATED = "repeated"                    # outro round apareceu
    PROVIDER_REJECTED = "provider_rejected"  # o provider recusou
    INCONCLUSIVE = "inconclusive"            # não foi possível decidir


# ---------------------------------------------------------------------------
# Estado do orquestrador — visão agregada de múltiplos rounds
# ---------------------------------------------------------------------------

class OrchestratorState(str, Enum):
    """Estado agregado ao final da orquestração.

    É o que o `ApplicationLoop` consome para decidir se continua ou cai em
    `NEEDS_HUMAN_CAPTCHA`.
    """

    RESOLVED = "resolved"
    HUMAN_REQUIRED = "human_required"
    FAILED = "failed"
    EXPIRED = "expired"
    UNSUPPORTED = "unsupported"


# ---------------------------------------------------------------------------
# Chaves tipadas — evitam strings mágicas em dicionários de capability
# ---------------------------------------------------------------------------

#: Nome canônico do provider (ex.: "recaptcha", "hcaptcha", "turnstile").
ProviderKey: TypeAlias = str

#: Nome canônico do tipo de challenge (ex.: "checkbox", "image_selection").
ChallengeTypeKey: TypeAlias = str

#: Tupla usada como chave da CapabilityMatrix.
CapabilityKey: TypeAlias = tuple[ProviderKey, ChallengeTypeKey]

#: Resultado agregado exposto publicamente (union dos estados terminais).
ResolutionOutcome: TypeAlias = Literal[
    "resolved",
    "human_required",
    "failed",
    "expired",
    "unsupported",
]

#: Sentinelas para valores desconhecidos.
UNKNOWN_PROVIDER: Final[ProviderKey] = "unknown"
UNKNOWN_CHALLENGE_TYPE: Final[ChallengeTypeKey] = "unknown"
