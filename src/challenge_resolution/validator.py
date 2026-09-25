"""`MonitorValidator`: transforma percepcao em veredito. Nada mais.

A Fase 2 do plano cabe em uma regra: reobservar e traduzir a decisao do guard
para o vocabulario de validacao, SEM introduzir estados novos na maquina do
guard. Este modulo nao interage com a UI: so observa.

Este modulo e uma TRADUCAO PURA: nao ha segunda politica aqui. O guard ja
consulta `browser_write_sent` por conta propria, e isso foi medido contra o
artefato fixado — a MESMA pagina de recusa produz:

    browser_write_sent=False -> decisao `observe`         (nada foi enviado)
    browser_write_sent=True  -> decisao `provider_rejected` (a candidatura saiu)

Ou seja: "recusa sem escrita" nao existe como entrada, e reescrever a decisao
aqui seria uma politica oculta (e dificil de auditar). O exactly-once continua
sendo do `NetworkWriteGuard`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import ValidationResult
from .protocols import ChallengeObserver
from .session import ChallengeSession
from .types import ChallengeDecisionStatus, ValidationStatus

#: Traducao decisao do guard -> veredito de validacao.
#:
#: Cobre TODOS os membros do `ChallengeDecisionStatus` do guard v0.1.0. Um
#: membro novo no guard nao entra em silencio: o teste celula-a-celula itera o
#: enum e falha se algum ficar de fora — o que forca a decisao de politica.
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
        page_errors: Sequence[str] = (),
    ) -> ValidationResult:
        observation = self.monitor.observe(  # type: ignore[attr-defined]
            phase=session.phase,
            browser_write_sent=browser_write_sent,
            submission_confirmed=submission_confirmed,
            # O guard casa marcadores de RESPOSTA (frases do provider) contra
            # este texto: é o que separa "recusou a candidatura" de "desafio
            # ainda na frente".
            response_texts=tuple(page_errors),
        )
        decision = self.monitor.decide(observation)  # type: ignore[attr-defined]
        decision_status = getattr(decision, "status", None)
        status = _DECISION_TO_STATUS.get(decision_status, ValidationStatus.INCONCLUSIVE)
        return ValidationResult(
            status=status,
            observation=observation,
            guard_decision_status=decision_status,
        )


__all__ = ["MonitorValidator"]
