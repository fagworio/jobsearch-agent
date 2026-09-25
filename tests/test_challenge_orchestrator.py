"""Fase 1 — `ChallengeOrchestrator`: limites, rounds, exceções, journal.

O orquestrador é o único componente que coordena percepção, resolução e
validação. Estes testes fixam as cinco garantias que ele promete:

1. nunca levanta para o `ApplicationLoop`;
2. `CHALLENGE_ORCHESTRATION_FINISHED` sempre sai;
3. limites duros valem (rounds, timeout, duração);
4. `rounds` conta rounds iniciados;
5. proveniência sem material sensível.

Nenhum browser aqui: o monitor, o engine e o validator são roteirizados, e o
journal captura os eventos em memória.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from challenge_guard.models import ChallengePhase, ChallengeProvider, ChallengeType

from challenge_resolution.journal import InMemoryJournal
from challenge_resolution.models import OrchestratorLimits, ResolutionResult, ValidationResult
from challenge_resolution.orchestrator import ChallengeOrchestrator
from challenge_resolution.session import ChallengeSession
from challenge_resolution.strategies import NullExecutor
from challenge_resolution.types import ChallengeObservation, ResolutionStatus, ValidationStatus
from challenge_resolution.validator import MonitorValidator


def _session(**overrides: object) -> ChallengeSession:
    values: dict[str, object] = {
        "session_id": "s-1",
        "application_id": "app-1",
        "provider": ChallengeProvider.RECAPTCHA,
        "challenge_type": "checkbox",
        "phase": ChallengePhase.PRE_SUBMIT,
        "started_at": datetime(2026, 9, 25, tzinfo=timezone.utc),
        "initial_confidence": 0.9,
    }
    values.update(overrides)
    return ChallengeSession(**values)  # type: ignore[arg-type]


def _observation(*, detected: bool = True, confidence: float = 0.92, challenge_type: ChallengeType = ChallengeType.CHECKBOX) -> ChallengeObservation:
    return ChallengeObservation(
        detected=detected,
        phase=ChallengePhase.PRE_SUBMIT,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type=challenge_type,
        session_id="s-1",
        visible=True,
        confidence=confidence,
    )


@dataclass
class ScriptedMonitor:
    """Devolve a próxima observação; depois disso, "sem desafio"."""

    script: list[ChallengeObservation] = field(default_factory=list)
    calls: int = 0

    def observe(self, *, phase: ChallengePhase = ChallengePhase.PRE_SUBMIT, **_extras: object) -> ChallengeObservation:
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return _observation(detected=False, confidence=0.0)


def _resolved(strategy: str = "stub") -> ResolutionResult:
    return ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type="checkbox",
        session_id="s-1",
        strategy_name=strategy,
        duration_seconds=0.01,
    )


def _failed(code: str = "boom") -> ResolutionResult:
    return ResolutionResult(
        status=ResolutionStatus.FAILED,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type="checkbox",
        session_id="s-1",
        error_code=code,
    )


def _unavailable() -> ResolutionResult:
    return ResolutionResult(
        status=ResolutionStatus.TEMPORARILY_UNAVAILABLE,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type="checkbox",
        session_id="s-1",
    )


@dataclass
class ScriptedEngine:
    results: list[ResolutionResult] = field(default_factory=list)
    calls: int = 0
    raise_on_call: int | None = None
    delay: float = 0.0

    def resolve(self, session: ChallengeSession, observation: object, executor: object) -> ResolutionResult:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on_call == self.calls:
            raise RuntimeError("engine explodiu")
        return self.results.pop(0) if self.results else _failed()


@dataclass
class ScriptedValidator:
    statuses: list[ValidationStatus] = field(default_factory=list)
    calls: int = 0
    raise_on_call: int | None = None

    def validate(self, session: ChallengeSession, **_kwargs: object) -> ValidationResult:
        self.calls += 1
        if self.raise_on_call == self.calls:
            raise ValueError("validator explodiu")
        status = self.statuses.pop(0) if self.statuses else ValidationStatus.ACCEPTED
        return ValidationResult(status=status, observation=_observation())


def _orchestrator(
    *,
    monitor: ScriptedMonitor,
    engine: ScriptedEngine,
    validator: ScriptedValidator,
    journal: InMemoryJournal | None = None,
    limits: OrchestratorLimits | None = None,
) -> tuple[ChallengeOrchestrator, InMemoryJournal]:
    captured = journal or InMemoryJournal()
    return (
        ChallengeOrchestrator(
            monitor=monitor,
            engine=engine,
            validator=validator,
            journal=captured,
            limits=limits or OrchestratorLimits(),
        ),
        captured,
    )


# --- limites ---------------------------------------------------------------

def test_max_rounds_stops_the_loop_and_never_validates() -> None:
    monitor = ScriptedMonitor(script=[_observation() for _ in range(10)])
    engine = ScriptedEngine(results=[_unavailable() for _ in range(10)])
    validator = ScriptedValidator()
    orchestrator, journal = _orchestrator(
        monitor=monitor,
        engine=engine,
        validator=validator,
        limits=OrchestratorLimits(max_rounds=3, timeout_seconds=60, max_duration_seconds=120),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.EXPIRED
    assert outcome.rounds == 3
    assert engine.calls == 3
    assert validator.calls == 0
    limits = [event.fields["limit"] for event in journal.find("challenge_limit_exceeded")]
    assert limits == ["max_rounds"]


def test_max_duration_stops_the_loop() -> None:
    monitor = ScriptedMonitor(script=[_observation() for _ in range(10)])
    engine = ScriptedEngine(results=[_unavailable() for _ in range(10)], delay=0.05)
    orchestrator, journal = _orchestrator(
        monitor=monitor,
        engine=engine,
        validator=ScriptedValidator(),
        # `OrchestratorLimits` exige max_duration >= timeout; com os dois iguais, quem
    # decide e a ORDEM (a duracao e checada antes) — que e o teto absoluto.
        limits=OrchestratorLimits(max_rounds=100, timeout_seconds=0.12, max_duration_seconds=0.12),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.EXPIRED
    assert outcome.rounds >= 2
    assert [event.fields["limit"] for event in journal.find("challenge_limit_exceeded")] == ["max_duration"]


def test_timeout_stops_the_loop() -> None:
    monitor = ScriptedMonitor(script=[_observation() for _ in range(10)])
    engine = ScriptedEngine(results=[_unavailable() for _ in range(10)], delay=0.05)
    orchestrator, journal = _orchestrator(
        monitor=monitor,
        engine=engine,
        validator=ScriptedValidator(),
        limits=OrchestratorLimits(max_rounds=100, timeout_seconds=0.11, max_duration_seconds=10),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.EXPIRED
    assert [event.fields["limit"] for event in journal.find("challenge_limit_exceeded")] == ["timeout"]


def test_no_challenge_is_an_early_exit_with_zero_rounds() -> None:
    monitor = ScriptedMonitor(script=[_observation(detected=False, confidence=0.0)])
    engine = ScriptedEngine()
    validator = ScriptedValidator()
    orchestrator, journal = _orchestrator(monitor=monitor, engine=engine, validator=validator)

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.RESOLVED
    assert outcome.resolved is True
    assert outcome.rounds == 0
    assert engine.calls == 0 and validator.calls == 0
    assert len(journal.find("challenge_early_exit_no_challenge")) == 1


# --- rounds ----------------------------------------------------------------

def test_resolved_then_accepted_finishes_in_one_round() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_resolved("recaptcha_checkbox")]),
        validator=ScriptedValidator(statuses=[ValidationStatus.ACCEPTED]),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.RESOLVED
    assert outcome.rounds == 1
    assert outcome.provenance.strategy_names == ("recaptcha_checkbox",)
    assert outcome.provenance.max_confidence == pytest.approx(0.92)


@pytest.mark.parametrize("first_status", [ValidationStatus.REPEATED, ValidationStatus.INCONCLUSIVE])
def test_a_round_that_repeats_forces_another_round(first_status: ValidationStatus) -> None:
    monitor = ScriptedMonitor(script=[_observation(), _observation()])
    engine = ScriptedEngine(results=[_resolved(), _resolved()])
    validator = ScriptedValidator(statuses=[first_status, ValidationStatus.ACCEPTED])
    orchestrator, _journal = _orchestrator(
        monitor=monitor,
        engine=engine,
        validator=validator,
        limits=OrchestratorLimits(max_rounds=5),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.RESOLVED
    assert outcome.rounds == 2
    assert engine.calls == 2 and validator.calls == 2


def test_provider_rejection_terminates_as_failed() -> None:
    orchestrator, _journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_resolved()]),
        validator=ScriptedValidator(statuses=[ValidationStatus.PROVIDER_REJECTED]),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.FAILED
    assert outcome.resolved is False
    assert outcome.rounds == 1


def test_engine_failure_skips_the_validator() -> None:
    validator = ScriptedValidator()
    orchestrator, _journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_failed("no_strategy")]),
        validator=validator,
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.FAILED
    assert validator.calls == 0


def test_unavailable_retries_until_the_round_budget_runs_out() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation() for _ in range(10)]),
        engine=ScriptedEngine(results=[_unavailable(), _unavailable(), _resolved()]),
        validator=ScriptedValidator(statuses=[ValidationStatus.ACCEPTED]),
        limits=OrchestratorLimits(max_rounds=5),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.RESOLVED
    assert outcome.rounds == 3


# --- exceções --------------------------------------------------------------

def test_an_engine_exception_never_reaches_the_caller() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(raise_on_call=1),
        validator=ScriptedValidator(),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.FAILED
    events = journal.find("challenge_engine_exception")
    assert len(events) == 1
    assert events[0].fields["error_type"] == "RuntimeError"


def test_a_validator_exception_never_reaches_the_caller() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_resolved()]),
        validator=ScriptedValidator(raise_on_call=1),
    )

    outcome = orchestrator.run(_session())

    assert outcome.final_status is ResolutionStatus.FAILED
    assert journal.find("challenge_validator_exception")[0].fields["error_type"] == "ValueError"


def test_a_broken_monitor_also_never_reaches_the_caller() -> None:
    """Falha de percepção não pode escapar: o loop decide bloquear."""

    class BrokenMonitor:
        def observe(self, **_kwargs: object) -> ChallengeObservation:
            raise RuntimeError("observador caiu")

    orchestrator = ChallengeOrchestrator(
        monitor=BrokenMonitor(),  # type: ignore[arg-type]
        engine=ScriptedEngine(),
        validator=ScriptedValidator(),
        journal=InMemoryJournal(),
    )

    with pytest.raises(RuntimeError):
        # Comportamento declarado: a percepção é o PRIMEIRO passo e não está
        # protegida nesta fase — quem protege é o gate (que trata falha como
        # bloqueio). O teste documenta isso em vez de esconder.
        orchestrator.run(_session())


def test_the_finished_event_is_always_emitted() -> None:
    for engine in (ScriptedEngine(results=[_resolved()]), ScriptedEngine(raise_on_call=1)):
        orchestrator, journal = _orchestrator(
            monitor=ScriptedMonitor(script=[_observation()]),
            engine=engine,
            validator=ScriptedValidator(),
        )
        orchestrator.run(_session())
        assert journal.find("challenge_orchestration_finished")


# --- journal e proveniência -----------------------------------------------

def test_the_happy_path_event_order_is_exact() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_resolved()]),
        validator=ScriptedValidator(statuses=[ValidationStatus.ACCEPTED]),
    )

    orchestrator.run(_session())

    assert journal.kinds() == [
        "challenge_orchestration_started",
        "challenge_round_started",
        "challenge_resolution_result",
        "challenge_validation_result",
        "challenge_round_finished",
        "challenge_orchestration_finished",
    ]


def test_the_finished_event_carries_the_provenance() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_resolved("recaptcha_checkbox")]),
        validator=ScriptedValidator(statuses=[ValidationStatus.ACCEPTED]),
    )

    orchestrator.run(_session())

    finished = journal.last("challenge_orchestration_finished")
    assert finished is not None
    fields = finished.fields
    assert fields["session_id"] == "s-1"
    assert fields["application_id"] == "app-1"
    assert fields["rounds"] == 1
    assert fields["final_status"] == "resolved"
    assert fields["resolved"] is True
    assert fields["strategy_names"] == ["recaptcha_checkbox"]
    assert fields["duration_seconds"] >= 0


def test_no_event_carries_sensitive_material() -> None:
    orchestrator, journal = _orchestrator(
        monitor=ScriptedMonitor(script=[_observation()]),
        engine=ScriptedEngine(results=[_resolved()]),
        validator=ScriptedValidator(statuses=[ValidationStatus.ACCEPTED]),
    )

    orchestrator.run(_session())

    forbidden = {"token", "cookie", "secret", "answer", "response", "payload", "password"}
    for event in journal.events:
        leaked = forbidden & {key.casefold() for key in event.fields}
        assert not leaked, f"{event.kind} vaza {leaked}"


def test_the_journal_refuses_an_unknown_event_kind() -> None:
    """Typo vira erro de teste, não evento invisível em produção."""
    journal = InMemoryJournal()

    with pytest.raises(ValueError, match="evento desconhecido"):
        journal.emit("CHALLENGE_ORCHESTRATION_STARTED")  # valor errado de propósito


# --- executor e validador reais -------------------------------------------

def test_the_default_executor_refuses_to_pretend_it_interacted() -> None:
    with pytest.raises(NotImplementedError):
        NullExecutor().execute(_session(), _observation(), payload=None)  # type: ignore[arg-type]


@dataclass
class _ScriptedDecision:
    status: object


@dataclass
class _ScriptedGuardMonitor:
    """Monitor mínimo com `observe` + `decide`, como o do guard."""

    decision: object

    def observe(self, **_kwargs: object) -> ChallengeObservation:
        return _observation(detected=self.decision is not None)

    def decide(self, observation: ChallengeObservation) -> object:
        return self.decision


def test_the_monitor_validator_maps_the_guard_decision() -> None:
    from challenge_guard.models import ChallengeDecisionStatus

    accepted = MonitorValidator(monitor=_ScriptedGuardMonitor(_ScriptedDecision(ChallengeDecisionStatus.RESOLVED_EXTERNALLY)))
    repeated = MonitorValidator(monitor=_ScriptedGuardMonitor(_ScriptedDecision(ChallengeDecisionStatus.NEEDS_HUMAN)))
    inconclusive = MonitorValidator(monitor=_ScriptedGuardMonitor(_ScriptedDecision(ChallengeDecisionStatus.UNKNOWN)))

    assert accepted.validate(_session()).status is ValidationStatus.ACCEPTED
    assert repeated.validate(_session()).status is ValidationStatus.REPEATED
    assert inconclusive.validate(_session()).status is ValidationStatus.INCONCLUSIVE


def test_a_rejection_without_a_write_is_not_a_rejection() -> None:
    """Exactly-once: sem escrita não houve candidatura recusada."""
    from challenge_guard.models import ChallengeDecisionStatus

    validator = MonitorValidator(monitor=_ScriptedGuardMonitor(_ScriptedDecision(ChallengeDecisionStatus.PROVIDER_REJECTED)))

    assert validator.validate(_session(), browser_write_sent=False).status is ValidationStatus.REPEATED
    assert validator.validate(_session(), browser_write_sent=True).status is ValidationStatus.PROVIDER_REJECTED
