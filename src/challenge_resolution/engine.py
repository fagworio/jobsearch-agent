"""`StrategyEngine`: roteia uma observacao para a estrategia certa.

Duas decisoes, nesta ordem:

1. **A capability decide primeiro.** Se o par (provider, tipo) e `UNSUPPORTED` ou
   `HUMAN_REQUIRED`, o engine nem consulta estrategia nenhuma: nao se tenta
   interagir com o que a politica declarou fora de alcance. E o que impede uma
   estrategia "generica" de tentar resolver um desafio de imagem.
2. **A primeira estrategia que suporta vence**, na ordem do registry.

Erro de estrategia nao propaga: vira `FAILED` com `error_code`. O orquestrador
decide o que fazer com isso.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .capabilities import CapabilityMatrix
from .models import ResolutionResult
from .protocols import ChallengeInteractionExecutor, ResolutionStrategy
from .session import ChallengeSession
from .strategies.base import StrategyRegistry
from .types import CapabilityStatus, ChallengeObservation, ResolutionStatus, enum_value


@dataclass
class StrategyEngine:
    """Implementacao padrao do `ChallengeResolutionEngine`."""

    strategies: Sequence[ResolutionStrategy] = ()
    capabilities: CapabilityMatrix = field(default_factory=CapabilityMatrix)

    def __post_init__(self) -> None:
        self._registry = StrategyRegistry(self.strategies)

    def resolve(
        self,
        session: ChallengeSession,
        observation: ChallengeObservation,
        executor: ChallengeInteractionExecutor,
    ) -> ResolutionResult:
        capability = self.capabilities.lookup(observation.provider, enum_value(observation.challenge_type))

        if capability is CapabilityStatus.HUMAN_REQUIRED:
            return self._result(session, observation, ResolutionStatus.HUMAN_REQUIRED, error_code="capability_human_required")
        if capability is CapabilityStatus.TEMPORARILY_UNAVAILABLE:
            return self._result(session, observation, ResolutionStatus.TEMPORARILY_UNAVAILABLE, error_code="capability_unavailable")
        if capability is not CapabilityStatus.SUPPORTED:
            return self._result(session, observation, ResolutionStatus.UNSUPPORTED, error_code="capability_unsupported")

        strategy = self._registry.select(observation)
        if strategy is None:
            return self._result(session, observation, ResolutionStatus.UNSUPPORTED, error_code="no_strategy")

        try:
            result = strategy.resolve(session, observation, executor)
        except Exception as exc:
            return self._result(
                session,
                observation,
                ResolutionStatus.FAILED,
                strategy_name=strategy.name,
                error_code=f"strategy_exception:{type(exc).__name__}",
            )
        # A estrategia pode devolver um resultado sem sessao/provedor preenchidos;
        # aqui garantimos a identidade minima para o journal.
        if not result.session_id:
            return ResolutionResult(
                status=result.status,
                provider=result.provider,
                challenge_type=result.challenge_type,
                session_id=session.session_id,
                strategy_name=result.strategy_name or strategy.name,
                duration_seconds=result.duration_seconds,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        return result

    @staticmethod
    def _result(
        session: ChallengeSession,
        observation: ChallengeObservation,
        status: ResolutionStatus,
        *,
        strategy_name: str | None = None,
        error_code: str | None = None,
    ) -> ResolutionResult:
        return ResolutionResult(
            status=status,
            provider=observation.provider,
            challenge_type=enum_value(observation.challenge_type),
            session_id=session.session_id,
            strategy_name=strategy_name,
            error_code=error_code,
        )


__all__ = ["StrategyEngine"]
