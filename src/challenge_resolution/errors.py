"""Hierarquia de exceções do pacote `challenge_resolution`.

Toda exceção carrega `session_id` quando aplicável, para correlação no journal.
Exceções NUNCA contêm material sensível em `args` ou atributos.
"""

from __future__ import annotations


class ChallengeResolutionError(Exception):
    """Base de todas as exceções do pacote."""

    def __init__(self, message: str, *, session_id: str | None = None) -> None:
        super().__init__(message)
        self.session_id = session_id


class StrategyNotFoundError(ChallengeResolutionError):
    """Nenhuma estratégia registrada suporta a observação."""


class StrategyExecutionError(ChallengeResolutionError):
    """A estratégia levantou exceção durante a execução."""

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None = None,
        strategy_name: str | None = None,
    ) -> None:
        super().__init__(message, session_id=session_id)
        self.strategy_name = strategy_name


class ExecutorError(ChallengeResolutionError):
    """O executor de interação falhou."""


class ExecutorUnavailableError(ExecutorError):
    """O executor não está disponível (ex.: browser fechado)."""


class ValidationError(ChallengeResolutionError):
    """O validador não conseguiu produzir veredito."""


class ProvenanceError(ChallengeResolutionError):
    """Proveniência inválida — tipicamente um campo sensível detectado.

    Este erro é SEMPRE levantado em construção, nunca engolido.
    """


class CapabilityViolationError(ChallengeResolutionError):
    """Uma operação violou a CapabilityMatrix.

    Indica bug de lógica, não erro operacional: uma estratégia só pode rodar
    para um par declarado `SUPPORTED`, e quem garante isso é o Engine.
    """
