"""Camada de resolução de challenges.

Este pacote NÃO substitui o `challenge-guard`. Ele depende dele para percepção
(observação, classificação, decisão) e adiciona a capacidade de **orquestrar**
tentativas de resolução.

Princípio que governa o pacote:

    O `challenge-guard` continua sendo olhos. O `challenge_resolution` é mãos,
    mas só as que o `jobsearch-agent` autoriza. E o `ApplicationLoop` continua
    sendo o único que decide se o POST sai.

Invariantes arquiteturais (verificados por
`tests/test_challenge_resolution_boundaries.py` e pelo import-linter):

  - nenhum módulo deste pacote importa de `jobsearch_agent`;
  - nenhum módulo importa `AuthorizedWrite`, `SubmissionIntent` ou
    `NetworkWriteGuard`;
  - nenhum dataclass daqui tem campo com material sensível (verificado no
    import por `provenance._assert_no_sensitive_fields`);
  - nada aqui emite evento nem toca browser: a Fase 0 é invisível em runtime.
"""

from .models import (
    CapabilityLookup,
    OrchestratorLimits,
    OrchestratorOutcome,
    ResolutionPayload,
    ResolutionResult,
    ValidationResult,
)
from .provenance import ChallengeProvenance
from .protocols import (
    ChallengeInteractionExecutor,
    ChallengeResolutionEngine,
    ChallengeResolutionOrchestrator,
    ChallengeResolutionValidator,
    ResolutionStrategy,
)
from .session import ChallengeSession
from .types import CapabilityStatus, ResolutionStatus, ValidationStatus

__all__ = [
    # types
    "CapabilityStatus",
    "ResolutionStatus",
    "ValidationStatus",
    # models
    "CapabilityLookup",
    "ResolutionPayload",
    "ResolutionResult",
    "ValidationResult",
    "OrchestratorOutcome",
    "OrchestratorLimits",
    # provenance / session
    "ChallengeProvenance",
    "ChallengeSession",
    # protocols
    "ChallengeResolutionEngine",
    "ChallengeInteractionExecutor",
    "ChallengeResolutionValidator",
    "ChallengeResolutionOrchestrator",
    "ResolutionStrategy",
]
