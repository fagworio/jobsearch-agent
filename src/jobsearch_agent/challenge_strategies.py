"""Estrategia e executor do caminho humano (relay), dentro do agente.

Fica no AGENTE, e nao no pacote de resolucao, por causa do contrato de camadas:
`challenge_resolution` nao pode importar `jobsearch_agent`, mas o agente pode
importar o pacote. O executor precisa do relay (`live_view`) e da pagina, que sao
do agente.

O que esta estrategia faz — e o que ela nao faz:

  - NAO clica no desafio, NAO digita, NAO copia token. Quem faz isso e a pessoa,
    por comandos que o relay transporta e executa na thread da pagina.
  - Ela abre a janela (o `ChallengeIntegration` desabilita o submit antes),
    espera o desafio sumir dentro de um orcamento, drenando os comandos do
    operador a cada rodada, e devolve o veredito do ROUND: `RESOLVED` quando o
    desafio saiu da pagina, `HUMAN_REQUIRED` quando o orcamento acabou.
  - A verificacao formal (o guard aceitou?) continua sendo do validador.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from challenge_resolution.models import ResolutionPayload, ResolutionResult
from challenge_resolution.protocols import ChallengeInteractionExecutor, ChallengeObserver
from challenge_resolution.session import ChallengeSession
from challenge_resolution.types import ChallengeObservation, ResolutionStatus
from .live_view import LiveViewRelay


@dataclass
class HumanRelayExecutor:
    """Espera a pessoa resolver, drenando os comandos dela na thread da pagina."""

    page: Any
    relay: LiveViewRelay
    monitor: ChallengeObserver
    wait_seconds: float = 30.0
    poll_seconds: float = 0.5
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def execute(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        payload: ResolutionPayload,
    ) -> None:
        deadline = self.clock() + max(float(self.wait_seconds), 0.0)
        while True:
            # Os comandos do operador executam AQUI: esta e a thread que possui a
            # pagina (a API sincrona do Playwright e presa a thread).
            self.relay.drain(self.page)
            if not self.monitor.observe(phase=session.phase).detected:
                return
            if self.clock() >= deadline:
                return
            self.sleep(self.poll_seconds)


@dataclass
class HumanRelayStrategy:
    """Estrategia de fallback: delega a uma pessoa e observa o efeito."""

    executor: HumanRelayExecutor
    supports_all: bool = True
    name: str = "human_relay"

    def supports(self, observation: ChallengeObservation) -> bool:
        return self.supports_all

    def resolve(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        executor: ChallengeInteractionExecutor,
    ) -> ResolutionResult:
        try:
            self.executor.execute(session, observation, ResolutionPayload(kind="handoff"))
        except Exception as exc:
            return ResolutionResult(
                status=ResolutionStatus.FAILED,
                provider=observation.provider,
                challenge_type=str(getattr(observation.challenge_type, "value", observation.challenge_type)),
                session_id=session.session_id,
                strategy_name=self.name,
                error_code=f"relay_executor:{type(exc).__name__}",
            )

        resolved = not self.executor.monitor.observe(phase=session.phase).detected
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED if resolved else ResolutionStatus.HUMAN_REQUIRED,
            provider=observation.provider,
            challenge_type=str(getattr(observation.challenge_type, "value", observation.challenge_type)),
            session_id=session.session_id,
            strategy_name=self.name,
            error_code="" if resolved else "human_did_not_resolve_in_budget",
        )


__all__ = ["HumanRelayExecutor", "HumanRelayStrategy"]
