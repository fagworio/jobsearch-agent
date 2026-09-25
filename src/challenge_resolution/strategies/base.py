"""Infraestrutura comum às estratégias.

`NullStrategy` é o placeholder da Fase 0/1 para que a Engine possa ser
instanciada sem nenhuma estratégia concreta: suporta tudo e não resolve nada.

`StrategyRegistry` é a lista **ordenada** consultada pela Engine. A ordem
importa: a primeira que `supports()` vence, e por isso ela é explícita na
construção — não há ordenação implícita por nome ou prioridade calculada.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from ..models import ResolutionPayload, ResolutionResult
from ..protocols import ChallengeInteractionExecutor, ResolutionStrategy
from ..session import ChallengeSession
from ..types import ChallengeObservation, ResolutionStatus, enum_value


class NullExecutor:
    """Executor nulo: nao interage com nada.

    Existe para que o orquestrador possa ser montado sem executor (testes e
    composicao sem browser). Tentar usar LEVANTA: um executor nulo que
    silenciosamente nao faz nada esconderia uma estrategia mal ligada.
    """

    def execute(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        payload: ResolutionPayload,
    ) -> None:
        raise NotImplementedError("NullExecutor nao interage. Entregue um executor real.")


class NullStrategy:
    """Estratégia nula. Suporta tudo, resolve nada.

    Uso: Fase 0/1, testes e fallback explícito. Sempre devolve `UNSUPPORTED` —
    nunca um sucesso inventado.
    """

    name: str = "null"

    def supports(self, observation: ChallengeObservation) -> bool:
        return True

    def resolve(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        executor: ChallengeInteractionExecutor,
    ) -> ResolutionResult:
        return ResolutionResult(
            status=ResolutionStatus.UNSUPPORTED,
            provider=observation.provider,
            challenge_type=enum_value(observation.challenge_type),
            session_id=session.session_id,
            strategy_name=self.name,
        )


class StrategyRegistry:
    """Lista ordenada de estratégias. Imutável após a construção."""

    def __init__(self, strategies: Iterable[ResolutionStrategy]) -> None:
        self._strategies: tuple[ResolutionStrategy, ...] = tuple(strategies)

    def select(self, observation: ChallengeObservation) -> ResolutionStrategy | None:
        for strategy in self._strategies:
            if strategy.supports(observation):
                return strategy
        return None

    def __iter__(self) -> Iterator[ResolutionStrategy]:
        return iter(self._strategies)

    def __len__(self) -> int:
        return len(self._strategies)

    def names(self) -> tuple[str, ...]:
        return tuple(strategy.name for strategy in self._strategies)
