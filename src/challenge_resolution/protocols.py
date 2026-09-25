"""Protocols do pacote `challenge_resolution`.

Define as fronteiras entre Engine, Executor, Validator e Orquestrador. Todos são
`Protocol` (structural typing), não ABCs — o que permite mocks em teste sem
herança.

Nenhum protocolo abaixo expõe `AuthorizedWrite`, `SubmissionIntent` ou
`NetworkWriteGuard`: a assinatura é a garantia de tipo de que a resolução não
consome o orçamento de submissão. Verificado por
`tests/test_challenge_resolution_boundaries.py` e pelo contrato do
import-linter.

DECISÃO DE RUNTIME (Fase 1): estes contratos são **síncronos**. O produto inteiro
é síncrono, a API do Playwright usada pelo agente é síncrona e presa à thread, e
o gate/relay que já rodam em produção são síncronos. Um contrato `async` só seria
executável movendo o loop (com journal e banco) para uma thread dona da sessão —
arquitetura que não foi decidida. Se ela for decidida, a mudança é mecânica:
quatro assinaturas em `protocols.py` mais as implementações.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .models import (
    OrchestratorOutcome,
    ResolutionPayload,
    ResolutionResult,
    ValidationResult,
)
from .session import ChallengeSession
from .types import ChallengeObservation, ChallengePhase


# ---------------------------------------------------------------------------
# Observador — percepção pura, satisfeita pelo `challenge-guard`
# ---------------------------------------------------------------------------

@runtime_checkable
class ChallengeObserver(Protocol):
    """Quem sabe responder "há desafio agora?".

    O `ChallengeMonitor` do `challenge-guard` satisfaz este Protocol sem
    adapter: `observe(self, *, phase=..., **extras)`. Declarar o Protocol (em vez
    de receber `object`) é o que permite `mypy --strict` cobrir o orquestrador —
    e não cria ciclo algum, porque o pacote já depende do guard.
    """

    def observe(
        self,
        *,
        phase: ChallengePhase = ChallengePhase.PRE_SUBMIT,
        browser_write_sent: bool = False,
        submission_confirmed: bool = False,
        response_texts: Sequence[str] = (),
    ) -> ChallengeObservation: ...


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

    def execute(
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

    def resolve(
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

    def resolve(
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

    `page_errors` é o canal de EVIDÊNCIA: o texto que a própria página mostrou
    (colhido pelo chamador). Sem ele, um envio entregue e recusado seria
    indistinguível de um desafio ainda pendente — medido contra o guard real:
    com a escrita e o texto, a decisão é `provider_rejected`; sem o texto, é
    `observe`. Quem tem o material em mãos é o chamador; o validator só repassa.
    """

    def validate(
        self,
        session: ChallengeSession,
        *,
        browser_write_sent: bool = False,
        submission_confirmed: bool = False,
        page_errors: Sequence[str] = (),
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

    def run(self, session: ChallengeSession) -> OrchestratorOutcome: ...
