"""`MonitorValidator`: transforma percepcao em veredito. Nada mais.

A Fase 2 do plano cabe em uma regra: reobservar e traduzir a decisao do guard
para o vocabulario de validacao, SEM introduzir estados novos na maquina do
guard. Este modulo nao interage com a UI: so observa.

Regra de exactly-once que ele carrega:

    `PROVIDER_REJECTED` so vale quando `browser_write_sent` e verdadeiro.

Sem isso, uma recusa atribuida a um desafio que apareceu ANTES de qualquer
escrita seria lida como "o provedor recusou a candidatura" — e o registro
passaria a afirmar que algo foi enviado quando nada saiu.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ValidationResult
from .protocols import ChallengeObserver
from .session import ChallengeSession
from .types import ChallengeDecisionStatus, ValidationStatus

#: Traducao decisao do guard -> veredito de validacao.
_DECISION_TO_STATUS: dict[ChallengeDecisionStatus, ValidationStatus] = {
    ChallengeDecisionStatus.RESOLVED_EXTERNALLY: ValidationStatus.ACCEPTED,
    ChallengeDecisionStatus.PROVIDER_REJECTED: ValidationStatus.PROVIDER_REJECTED,
    ChallengeDecisionStatus.NEEDS_HUMAN: ValidationStatus.REPEATED,
    ChallengeDecisionStatus.OBSERVE: ValidationStatus.INCONCLUSIVE,
    ChallengeDecisionStatus.NONE: ValidationStatus.ACCEPTED,
    ChallengeDecisionStatus.UNKNOWN: ValidationStatus.INCONCLUSIVE,
}


@dataclass
class MonitorValidator:
    """Valida reobservando o estado pelo monitor (o guard real, em producao)."""

    #: Observador com `observe(...) -> ChallengeObservation` e, quando houver,
    #: `decide(observation) -> ChallengeDecision`. O monitor do guard tem os dois.
    monitor: object

    def validate(
        self,
        session: ChallengeSession,
        *,
        browser_write_sent: bool = False,
        submission_confirmed: bool = False,
    ) -> ValidationResult:
        observation = self.monitor.observe(  # type: ignore[attr-defined]
            phase=session.phase,
            browser_write_sent=browser_write_sent,
            submission_confirmed=submission_confirmed,
        )
        decision = self.monitor.decide(observation)  # type: ignore[attr-defined]
        status = _DECISION_TO_STATUS.get(getattr(decision, "status", None), ValidationStatus.INCONCLUSIVE)
        if status is ValidationStatus.PROVIDER_REJECTED and not browser_write_sent:
            # Recusa sem escrita nao e recusa de candidatura: e desafio pendente.
            status = ValidationStatus.REPEATED
        return ValidationResult(status=status, observation=observation)


__all__ = ["MonitorValidator"]
