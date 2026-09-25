"""Durabilidade da janela do operador: o que sobra quando o processo morre.

O relay é o caminho de produção, e o serviço de handoff completo (webhook
assinado, fila durável, operador remoto) não existe. Isso deixa uma lacuna que
precisa ser dita em voz alta: **um restart no meio da resolução perde a pessoa**
— o browser não volta, e o trabalho dela se perde.

O que estes testes garantem é o que É possível garantir sem o serviço:

1. a abertura da janela fica **gravada** (`challenge_handoff_started`) com
   sessão, provider, tipo e orçamento — antes disso, um crash não deixava rastro
   nenhum de que alguém foi chamado;
2. o fechamento também (`challenge_handoff_finished`);
3. uma janela aberta sem fechamento é **reconciliada** no início da execução
   seguinte, com motivo explícito (`process_restarted_with_window_open`);
4. a reconciliação é idempotente e nunca derruba o loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.challenge_integration import (
    HANDOFF_ABANDONED,
    HANDOFF_FINISHED,
    HANDOFF_STARTED,
    reconcile_abandoned_handoffs,
)
from jobsearch_agent.models import Job
from jobsearch_agent.persistence import Database


def _application(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "durability.db")
    job = Job(
        id="job-durability",
        source="greenhouse",
        external_id="1",
        company="Acme",
        title="Engineer",
        description="Build things.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    database.save_job(job, "greenhouse:1", {})
    application = ApplicationService(database).create_for_job(job.id)
    return database, application.id


def _events(database: Database, application_id: str) -> list[tuple[str, dict]]:
    return [(event.event, dict(event.payload)) for event in database.list_application_events(application_id)]


def test_the_window_evidence_survives_without_changing_the_state(tmp_path: Path) -> None:
    database, application_id = _application(tmp_path)
    state_before = database.get_application(application_id).state

    database.append_application_event(application_id, HANDOFF_STARTED, {"session_id": "s-1", "provider": "recaptcha"})

    assert database.get_application(application_id).state is state_before
    assert _events(database, application_id) == [(HANDOFF_STARTED, {"session_id": "s-1", "provider": "recaptcha"})]


def test_a_window_left_open_by_a_restart_is_reconciled_with_a_reason(tmp_path: Path) -> None:
    database, application_id = _application(tmp_path)
    database.append_application_event(
        application_id,
        HANDOFF_STARTED,
        {"session_id": "s-1", "provider": "recaptcha", "challenge_type": "checkbox", "wait_seconds": 30.0},
    )

    reconciled = reconcile_abandoned_handoffs(database, application_id)

    assert reconciled == 1
    event, payload = _events(database, application_id)[-1]
    assert event == HANDOFF_ABANDONED
    assert payload["reason"] == "process_restarted_with_window_open"
    assert payload["session_id"] == "s-1"
    assert payload["provider"] == "recaptcha"


def test_reconciliation_is_idempotent(tmp_path: Path) -> None:
    database, application_id = _application(tmp_path)
    database.append_application_event(application_id, HANDOFF_STARTED, {"session_id": "s-1"})

    assert reconcile_abandoned_handoffs(database, application_id) == 1
    # A segunda chamada não encontra janela aberta: o abandono já fechou.
    assert reconcile_abandoned_handoffs(database, application_id) == 0
    assert len([1 for event, _ in _events(database, application_id) if event == HANDOFF_ABANDONED]) == 1


def test_a_window_that_closed_normally_is_not_reconciled(tmp_path: Path) -> None:
    database, application_id = _application(tmp_path)
    database.append_application_event(application_id, HANDOFF_STARTED, {"session_id": "s-1"})
    database.append_application_event(application_id, HANDOFF_FINISHED, {"resolved": True, "rounds": 1})

    assert reconcile_abandoned_handoffs(database, application_id) == 0
    assert HANDOFF_ABANDONED not in [event for event, _ in _events(database, application_id)]


def test_a_second_crash_after_a_reconciled_window_is_also_reconciled(tmp_path: Path) -> None:
    """Cada janela é uma janela: a reconciliação não pode marcar só a primeira."""
    database, application_id = _application(tmp_path)
    database.append_application_event(application_id, HANDOFF_STARTED, {"session_id": "s-1"})
    assert reconcile_abandoned_handoffs(database, application_id) == 1
    database.append_application_event(application_id, HANDOFF_STARTED, {"session_id": "s-2"})

    assert reconcile_abandoned_handoffs(database, application_id) == 1
    assert [payload["session_id"] for event, payload in _events(database, application_id) if event == HANDOFF_ABANDONED] == ["s-1", "s-2"]


def test_reconciliation_never_raises_on_an_empty_history(tmp_path: Path) -> None:
    database, application_id = _application(tmp_path)

    assert reconcile_abandoned_handoffs(database, application_id) == 0


def test_reconciliation_never_raises_when_the_database_is_broken() -> None:
    class _Broken:
        def list_application_events(self, _application_id: str):
            raise RuntimeError("banco fora")

    assert reconcile_abandoned_handoffs(_Broken(), "application-1") == 0


def test_the_integration_records_the_window_when_it_has_a_database(tmp_path: Path) -> None:
    """A janela é gravada ANTES da orquestração e fechada depois."""
    from challenge_resolution.models import OrchestratorOutcome, ResolutionStatus
    from challenge_resolution.provenance import ChallengeProvenance
    from challenge_resolution.types import ChallengePhase, ChallengeProvider
    from challenge_resolution.session import ChallengeSession
    from datetime import datetime, timezone

    from challenge_resolution.types import ChallengeObservation, ChallengeType

    from jobsearch_agent.challenge_integration import ChallengeIntegration

    database, application_id = _application(tmp_path)

    class _Observer:
        def __init__(self, detected: bool) -> None:
            self._detected = detected

        def observe(self, **_kwargs: object) -> ChallengeObservation:
            return ChallengeObservation(
                detected=self._detected,
                phase=ChallengePhase.PRE_SUBMIT,
                provider=ChallengeProvider.RECAPTCHA,
                challenge_type=ChallengeType.CHECKBOX,
                session_id="s-1",
                confidence=0.9,
            )

        def detach(self) -> None:
            return None

    class _Orchestrator:
        def run(self, session: ChallengeSession) -> OrchestratorOutcome:
            now = datetime.now(timezone.utc)
            return OrchestratorOutcome(
                resolved=True,
                rounds=1,
                duration_seconds=0.5,
                final_status=ResolutionStatus.RESOLVED,
                provenance=ChallengeProvenance(
                    session_id=session.session_id,
                    application_id=session.application_id,
                    provider=session.provider.value,
                    challenge_type=session.challenge_type,
                    rounds=1,
                    started_at=now,
                    finished_at=now,
                    final_status=ResolutionStatus.RESOLVED.value,
                    max_confidence=0.9,
                ),
            )

    integration = ChallengeIntegration(
        observer_factory=lambda page: _Observer(True),
        orchestrator_factory=lambda observer: _Orchestrator(),
        database=database,
        wait_seconds=30.0,
    )

    result = integration.handle(object(), application_id)

    assert result.handling.value == "continue"
    events = [event for event, _ in _events(database, application_id)]
    assert events == [HANDOFF_STARTED, HANDOFF_FINISHED]
    started = _events(database, application_id)[0][1]
    assert started["provider"] == "recaptcha"
    assert started["wait_seconds"] == 30.0


def test_no_challenge_means_no_window_at_all(tmp_path: Path) -> None:
    from challenge_resolution.types import ChallengeObservation, ChallengePhase, ChallengeProvider, ChallengeType

    from jobsearch_agent.challenge_integration import ChallengeIntegration

    database, application_id = _application(tmp_path)

    class _Observer:
        def observe(self, **_kwargs: object) -> ChallengeObservation:
            return ChallengeObservation(
                detected=False,
                phase=ChallengePhase.PRE_SUBMIT,
                provider=ChallengeProvider.UNKNOWN,
                challenge_type=ChallengeType.UNKNOWN,
                session_id="",
                confidence=0.0,
            )

        def detach(self) -> None:
            return None

    integration = ChallengeIntegration(
        observer_factory=lambda page: _Observer(),
        orchestrator_factory=lambda observer: pytest.fail("não deveria orquestrar sem desafio"),
        database=database,
    )

    result = integration.handle(object(), application_id)

    assert result.handling.value == "not_detected"
    assert _events(database, application_id) == []
