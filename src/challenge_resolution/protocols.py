"""Protocols do pacote `challenge_resolution`.

Define as fronteiras entre Engine, Executor, Validator e Orquestrador. Todos são
`Protocol` (structural typing), não ABCs — o que permite mocks em teste sem
herança.

Nenhum protocolo abaixo expõe `AuthorizedWrite`, `SubmissionIntent` ou
`NetworkWriteGuard`: a assinatura é a garantia de tipo de que a resolução não
consome o orçamento de submissão. Verificado por
`tests/test_challenge_resolution_boundaries.py` e pelo contrato do
import-linter.

Nota de runtime (decidir antes da Fase 1): estes contratos são `async`, como no
plano, mas o executor concreto fala com a **API síncrona** do Playwright, que
levanta erro se chamada dentro de um event loop. A Fase 1 precisa escolher:
executor em thread (`asyncio.to_thread`), orquestrador síncrono, ou migração
para a API async do Playwright. O contrato não esconde essa decisão.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    OrchestratorOutcome,
    ResolutionPayload,
    ResolutionResult,
    ValidationResult,
)
from .session import ChallengeSession
from .types import ChallengeObservation


# ---------------------------------------------------------------------------
# Executor de interação — única superfície que toca a UI do provider
# ---------------------------------------------------------------------------

@runtime_checkable
class ChallengeInteractionExecutor(Protocol):
    """Executa a interação física com a UI do provider.

    Contrato:

      - opera na MESMA sessão de browser que abriu a candidatura;
      - NUNCA emite POST para o endpoint de submissão;
      - NUNCA retém `payload.data` além do escopo da chamada;
      - retorna quando a interação estiver concluída (sucesso ou não);
      - propaga falhas como exceções tipadas de `errors.py`.

    A assinatura NÃO recebe `AuthorizedWrite`.
    """

    async def execute(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        payload: ResolutionPayload,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Estratégia de resolução — específica por provider/tipo
# ---------------------------------------------------------------------------

@runtime_checkable
class ResolutionStrategy(Protocol):
    """Estratégia concreta de resolução para um provider/tipo.

    Estratégias são plugáveis e o Engine escolhe uma por `supports()`.
    Estratégias NÃO decidem sozinhas se devem rodar: a `CapabilityMatrix` já
    filtrou antes de chegar aqui.
    """

    @property
    def name(self) -> str: ...

    def supports(self, observation: ChallengeObservation) -> bool: ...

    async def resolve(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        executor: ChallengeInteractionExecutor,
    ) -> ResolutionResult: ...


# ---------------------------------------------------------------------------
# Engine — roteamento entre estratégias
# ---------------------------------------------------------------------------

@runtime_checkable
class ChallengeResolutionEngine(Protocol):
    """Roteia uma observação para a estratégia correta.

    Consulta a `CapabilityMatrix` antes de despachar. Nunca levanta exceção para
    o orquestrador: erro de estratégia vira `ResolutionResult` com status
    apropriado.
    """

    async def resolve(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        executor: ChallengeInteractionExecutor,
    ) -> ResolutionResult: ...


# ---------------------------------------------------------------------------
# Validator — reobservação pós-tentativa
# ---------------------------------------------------------------------------

@runtime_checkable
class ChallengeResolutionValidator(Protocol):
    """Reobserva o estado após uma tentativa e produz um veredito.

    Implementações concretas usam o `ChallengeMonitor` do guard. O validator NÃO
    interage com a UI — apenas observa.
    """

    async def validate(
        self,
        session: ChallengeSession,
        *,
        browser_write_sent: bool = False,
        submission_confirmed: bool = False,
    ) -> ValidationResult: ...


# ---------------------------------------------------------------------------
# Orquestrador — o loop de múltiplos rounds
# ---------------------------------------------------------------------------

@runtime_checkable
class ChallengeResolutionOrchestrator(Protocol):
    """Executa o ciclo resolução → validação até conclusão ou limite.

    Consumido pelo `ApplicationLoop`. Não toca browser diretamente: delega a
    Engine, Executor e Validator.
    """

    async def run(self, session: ChallengeSession) -> OrchestratorOutcome: ...
