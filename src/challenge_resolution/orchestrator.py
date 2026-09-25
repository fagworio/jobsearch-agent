"""`ChallengeOrchestrator`: o loop que coordena percepcao, resolucao e validacao.

    observar -> engine.resolve -> validator.validate -> (retry | terminal)

Cinco garantias, todas testadas:

1. **Nunca levanta para o `ApplicationLoop`.** Excecao de engine ou validator
   vira `FAILED` com evento proprio; falha ao construir a proveniencia tem
   caminho de fallback.
2. **`CHALLENGE_ORCHESTRATION_FINISHED` sempre sai**, inclusive quando o
   engine levanta — ele e emitido no `finally`.
3. **Limites duros**: `max_rounds`, `timeout_seconds` e `max_duration_seconds`
   (todos medidos desde o inicio da orquestracao).
4. **`rounds` conta rounds INICIADOS**: sair porque o desafio desapareceu antes
   do primeiro round da `rounds=0`.
5. **Proveniencia sem segredo**: so provider, tipo, contagem, tempos e
   confianca — nada de token, cookie ou resposta de desafio.

Diferenca deliberada em relacao ao rascunho da Fase 1 (e a unica que muda a
assinatura): este componente e **sincrono**. O produto inteiro e sincrono, a API
do Playwright usada pelo agente e sincrona e presa a thread, e o gate/relay que
ja rodam em producao sao sincronos. Um orquestrador `async` so seria executavel
movendo o loop (com journal e banco) para uma thread dona da sessao — decisao de
arquitetura que ainda nao foi tomada. Os contratos da Fase 0 foram ajustados na
mesma direcao (ver `protocols.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import events as ev
from .journal import Journal, NullJournal
from .models import (
    OrchestratorLimits,
    OrchestratorOutcome,
    ResolutionResult,
    ValidationResult,
)
from .protocols import (
    ChallengeInteractionExecutor,
    ChallengeObserver,
    ChallengeResolutionEngine,
    ChallengeResolutionValidator,
)
from .provenance import ChallengeProvenance
from .session import ChallengeSession
from .strategies.base import NullExecutor
from .types import ChallengeObservation, ResolutionStatus, enum_value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChallengeOrchestrator:
    """Implementacao do protocolo `ChallengeResolutionOrchestrator`.

    Sem estado entre chamadas: todo o estado de uma orquestracao vive em
    `run()`. Duas sessoes podem ser orquestradas em sequencia sem heranca.
    """

    monitor: ChallengeObserver
    engine: ChallengeResolutionEngine
    validator: ChallengeResolutionValidator
    journal: Journal = field(default_factory=NullJournal)
    limits: OrchestratorLimits = field(default_factory=OrchestratorLimits)
    #: Executor da interacao. Fica explicito no construtor: o orquestrador nao
    #: fabrica um executor "de mentira" para o engine usar — quem interage e
    #: quem o chamador entregar (na Fase 6, o executor do browser).
    executor: ChallengeInteractionExecutor = field(default_factory=NullExecutor)

    def run(self, session: ChallengeSession) -> OrchestratorOutcome:
        started_at = _utcnow()
        t0 = time.monotonic()
        self.journal.emit(ev.CHALLENGE_ORCHESTRATION_STARTED, **session.as_journal())

        rounds = 0
        max_confidence = session.initial_confidence
        strategy_names: set[str] = set()
        final_status = ResolutionStatus.EXPIRED
        try:
            while True:
                elapsed = time.monotonic() - t0
                if elapsed >= self.limits.max_duration_seconds:
                    self._limit(limit="max_duration", elapsed=elapsed, session=session, rounds=rounds)
                    break

                observation = self.monitor.observe(phase=session.phase)
                if not observation.detected:
                    self.journal.emit(
                        ev.CHALLENGE_EARLY_EXIT_NO_CHALLENGE,
                        session_id=session.session_id,
                        rounds=rounds,
                    )
                    final_status = ResolutionStatus.RESOLVED
                    break

                if rounds >= self.limits.max_rounds:
                    self._limit(limit="max_rounds", elapsed=elapsed, session=session, rounds=rounds)
                    break
                if elapsed >= self.limits.timeout_seconds:
                    self._limit(limit="timeout", elapsed=elapsed, session=session, rounds=rounds)
                    break

                rounds += 1
                self.journal.emit(
                    ev.CHALLENGE_ROUND_STARTED,
                    session_id=session.session_id,
                    round=rounds,
                    provider=observation.provider.value,
                    challenge_type=enum_value(observation.challenge_type),
                )
                max_confidence = max(max_confidence, float(getattr(observation, "confidence", 0.0)))

                result = self._resolve(session, observation, rounds)
                if result is None:
                    final_status = ResolutionStatus.FAILED
                    break
                if result.strategy_name:
                    strategy_names.add(result.strategy_name)

                self.journal.emit(
                    ev.CHALLENGE_RESOLUTION_RESULT,
                    session_id=session.session_id,
                    round=rounds,
                    status=result.status.value,
                    strategy=result.strategy_name,
                    duration_seconds=round(float(result.duration_seconds), 3),
                    error_code=result.error_code,
                )

                if result.status is ResolutionStatus.TEMPORARILY_UNAVAILABLE:
                    self._round_finished(session, rounds, "retry")
                    continue
                if result.status is not ResolutionStatus.RESOLVED:
                    final_status = result.status
                    break

                validation = self._validate(session, rounds)
                if validation is None:
                    final_status = ResolutionStatus.FAILED
                    break

                self.journal.emit(
                    ev.CHALLENGE_VALIDATION_RESULT,
                    session_id=session.session_id,
                    round=rounds,
                    status=validation.status.value,
                )

                if validation.accepted:
                    self._round_finished(session, rounds, "accepted")
                    final_status = ResolutionStatus.RESOLVED
                    break
                if validation.provider_rejected:
                    self._round_finished(session, rounds, "provider_rejected")
                    final_status = ResolutionStatus.FAILED
                    break
                # REPEATED | INCONCLUSIVE: outro round.
                self._round_finished(session, rounds, validation.status.value)
        finally:
            outcome = self._build_outcome(
                session=session,
                started_at=started_at,
                rounds=rounds,
                confidence=max_confidence,
                strategy_names=tuple(sorted(strategy_names)),
                final_status=final_status,
            )
            self.journal.emit(
                ev.CHALLENGE_ORCHESTRATION_FINISHED,
                **outcome.provenance.as_journal(),
                resolved=outcome.resolved,
            )
        return outcome

    # -- passos que podem falhar sem derrubar o loop -------------------------

    def _resolve(
        self, session: ChallengeSession, observation: ChallengeObservation, rounds: int
    ) -> ResolutionResult | None:
        """Chama o engine. Excecao vira `None` (o chamador decide o status)."""
        try:
            return self.engine.resolve(session, observation, self.executor)
        except Exception as exc:
            self.journal.emit(
                ev.CHALLENGE_ENGINE_EXCEPTION,
                session_id=session.session_id,
                round=rounds,
                error_type=type(exc).__name__,
            )
            return None

    def _validate(self, session: ChallengeSession, rounds: int) -> ValidationResult | None:
        """Chama o validator. Excecao vira `None`."""
        try:
            return self.validator.validate(session)
        except Exception as exc:
            self.journal.emit(
                ev.CHALLENGE_VALIDATOR_EXCEPTION,
                session_id=session.session_id,
                round=rounds,
                error_type=type(exc).__name__,
            )
            return None

    # -- journal e proveniencia ---------------------------------------------

    def _limit(self, *, limit: str, elapsed: float, session: ChallengeSession, rounds: int) -> None:
        self.journal.emit(
            ev.CHALLENGE_LIMIT_EXCEEDED,
            limit=limit,
            elapsed_seconds=round(elapsed, 3),
            rounds=rounds,
            max_rounds=self.limits.max_rounds,
            timeout_seconds=self.limits.timeout_seconds,
            max_duration_seconds=self.limits.max_duration_seconds,
            session_id=session.session_id,
        )

    def _round_finished(self, session: ChallengeSession, rounds: int, outcome: str) -> None:
        self.journal.emit(
            ev.CHALLENGE_ROUND_FINISHED,
            session_id=session.session_id,
            round=rounds,
            outcome=outcome,
        )

    def _build_outcome(
        self,
        *,
        session: ChallengeSession,
        started_at: datetime,
        rounds: int,
        confidence: float,
        strategy_names: tuple[str, ...],
        final_status: ResolutionStatus,
    ) -> OrchestratorOutcome:
        """Proveniencia validada, com fallback: a construcao NUNCA levanta."""
        finished_at = _utcnow()
        try:
            provenance = ChallengeProvenance(
                session_id=session.session_id,
                application_id=session.application_id,
                provider=session.provider.value,
                challenge_type=session.challenge_type,
                rounds=max(int(rounds), 0),
                started_at=started_at,
                finished_at=finished_at,
                final_status=final_status.value,
                max_confidence=min(max(float(confidence), 0.0), 1.0),
                strategy_names=strategy_names,
            )
        except Exception:  # pragma: no cover - defensivo: proveniencia e dado validado
            finished_at = started_at
            provenance = ChallengeProvenance(
                session_id=session.session_id,
                application_id=session.application_id,
                provider=session.provider.value,
                challenge_type=session.challenge_type,
                rounds=0,
                started_at=started_at,
                finished_at=finished_at,
                final_status=ResolutionStatus.FAILED.value,
                max_confidence=0.0,
            )
            final_status = ResolutionStatus.FAILED
        return OrchestratorOutcome(
            resolved=final_status is ResolutionStatus.RESOLVED,
            rounds=provenance.rounds,
            duration_seconds=max((finished_at - started_at).total_seconds(), 0.0),
            final_status=final_status,
            provenance=provenance,
        )


__all__ = ["ChallengeOrchestrator"]
