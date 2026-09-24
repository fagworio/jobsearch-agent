"""JSA-LOOP-001: decisoes do loop, sem browser.

O caminho feliz de ponta a ponta esta em `tests/e2e/`. Aqui ficam as regras que
nao podem depender de um Chromium: quando o loop PARA sem tentar de novo, quando
ele para pedindo contexto, e o que ele recusa fazer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.loop import (
    NO_RESEND_STATES,
    RECOVERABLE_STATES,
    TERMINAL_STATES,
    ApplicationLoop,
    LoopRuntime,
    PreparedMaterial,
)
from jobsearch_agent.models import (
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    CandidatePreferences,
    CareerProfile,
    ConfirmationSource,
    Experience,
    Job,
    SubmissionConfirmationEvidence,
)
from jobsearch_agent.orchestrator import DryRunCycle, LiveApplicationResult
from jobsearch_agent.persistence import Database
from jobsearch_agent.qa import AnswerKnowledgeBase


class FakeSession:
    """Sessao minima: o loop so exige que exista e que possa ser fechada."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _job(database: Database) -> Job:
    job = Job(
        id="job-loop",
        source="greenhouse",
        external_id="1",
        company="Acme",
        title="WordPress Developer",
        description="Build WordPress sites.",
        url="https://boards.greenhouse.io/acme/jobs/1",
    )
    database.save_job(job, "greenhouse:1", {})
    return job


def _advance(service: ApplicationService, application_id: str, *states: ApplicationState) -> None:
    for state in states:
        service.transition(application_id, state, f"to_{state.value.casefold()}")


def _to_submitted(service: ApplicationService, application_id: str) -> None:
    _advance(
        service,
        application_id,
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
        ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED,
        ApplicationState.SUBMITTING,
    )
    service.transition(
        application_id,
        ApplicationState.SUBMITTED,
        "submission_confirmed",
        confirmation_evidence=SubmissionConfirmationEvidence(
            source=ConfirmationSource.PROVIDER_APPLICATION_STATUS,
            observed_at="2026-09-24T16:00:00+00:00",
            reference="status-1",
            provider="greenhouse",
        ),
    )


def _to_unknown(service: ApplicationService, application_id: str) -> None:
    _advance(
        service,
        application_id,
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
        ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED,
        ApplicationState.SUBMITTING,
        ApplicationState.SUBMIT_UNKNOWN,
    )


def _to_awaiting(service: ApplicationService, application_id: str) -> None:
    _advance(
        service,
        application_id,
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
        ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED,
        ApplicationState.SUBMITTING,
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
    )
    service.start_human_handoff(application_id)
    service.report_manual_submission(application_id)


def _runtime(*, live: LiveApplicationResult | None = None, session: FakeSession | None = None) -> LoopRuntime:
    """Runtime de teste. `prepare`/`open_session` podem explodir de proposito."""

    def prepare(job: Job) -> PreparedMaterial:
        return PreparedMaterial(
            resume_path="/tmp/does-not-matter.pdf",
            resume_sha256="a" * 64,
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        )

    return LoopRuntime(
        adapter_for=lambda job: object(),  # type: ignore[arg-type]
        form_url=lambda job, adapter: job.url,
        profile=CareerProfile(
            identity={"full_name": "Loop Candidate"},
            professional_summary={},
            experiences=[Experience(id="e1", company="Acme", role="Dev", start_date="2020-01")],
            skills={},
            languages={},
            demo=False,
        ),
        preferences=CandidatePreferences(),
        answers=AnswerKnowledgeBase([]),
        prepare=prepare,
        open_session=lambda job, adapter: session or FakeSession(),
    )


def _forbidden_runtime(message: str) -> LoopRuntime:
    """Runtime que falha se o loop tentar preparar material ou abrir browser."""

    def explode(*_args, **_kwargs):
        raise AssertionError(message)

    base = _runtime()
    return LoopRuntime(
        adapter_for=explode,
        form_url=explode,
        profile=base.profile,
        preferences=base.preferences,
        answers=base.answers,
        prepare=explode,
        open_session=explode,
    )


@pytest.mark.parametrize(
    "advance, expected_status, terminal",
    [
        (_to_submitted, "ALREADY_SUBMITTED", True),
        (_to_unknown, "NO_RESEND", True),
        # Nunca reenviar NAO e o mesmo que terminal: por evidencia independente
        # esta Application ainda chega a SUBMITTED.
        (_to_awaiting, "NO_RESEND", False),
    ],
)
def test_a_decided_application_never_reopens_the_browser(
    tmp_path: Path, advance, expected_status: str, terminal: bool
):
    """LOOP-16: reenvio e o unico erro que nao se corrige depois.

    Nada de preparar material, abrir browser ou POST: o loop para na porta.
    """
    database = Database(tmp_path / "loop.db")
    service = ApplicationService(database)
    job = _job(database)
    application = service.create_for_job(job.id)
    advance(service, application.id)

    result = ApplicationLoop(database, _forbidden_runtime("o rerun tentou executar o fluxo")).run(
        job.id, submit=True
    )

    assert result.status == expected_status
    assert result.terminal is terminal
    assert result.requires_action == ("" if terminal else result.state.value)
    assert result.submission_attempted is False
    assert result.submission_writes == 0
    assert result.phases == ()
    database.close()


def test_the_loop_stops_on_an_unanswered_required_field(tmp_path: Path, monkeypatch):
    """Sem fato objetivo nao se inventa: o loop para e diz o que falta."""
    database = Database(tmp_path / "loop.db")
    service = ApplicationService(database)
    job = _job(database)
    service.create_for_job(job.id)

    form = ApplicationForm(
        form_id="form-loop",
        provider="greenhouse",
        fields=[
            ApplicationField(key="job_application[first_name]", label="First name", required=True, value="Loop"),
            ApplicationField(key="job_application[unknown]", label="Invented metric?", required=True, value=""),
        ],
    )
    live = LiveApplicationResult(
        "NEEDS_ANSWER",
        provider="greenhouse",
        url=job.url,
        cycles=[DryRunCycle(1, "fingerprint", ApplicationState.NEEDS_ANSWER)],
        form=form,
        form_fingerprint="fingerprint",
        error="unknown_answer:job_application[unknown]",
    )
    monkeypatch.setattr("jobsearch_agent.loop.LiveApplicationOrchestrator", lambda *a, **k: _FakeOrchestrator(live))

    result = ApplicationLoop(database, _runtime(live=live)).run(job.id, submit=True)

    assert result.status == "NEEDS_ANSWER"
    assert result.terminal is False
    assert result.requires_action == ApplicationState.NEEDS_ANSWER.value
    assert result.submission_attempted is False
    assert result.unanswered_required == ("Invented metric?",)
    assert result.questions_answered == 1
    assert result.reason == "unknown_answer:job_application[unknown]"
    # A decisao do Safety Gate ficou PERSISTIDA: sem isto o caminho de falha nao
    # deixava rastro e o loop nao tinha estado para reportar.
    assert database.get_application(result.application_id).state is ApplicationState.NEEDS_ANSWER
    database.close()


def test_a_repeated_form_is_terminal_and_not_retried(tmp_path: Path, monkeypatch):
    """`LOOP_DETECTED` e defeito de fluxo, nao convite a tentar de novo."""
    database = Database(tmp_path / "loop.db")
    service = ApplicationService(database)
    job = _job(database)
    service.create_for_job(job.id)

    live = LiveApplicationResult("LOOP_DETECTED", provider="greenhouse", url=job.url, error="fingerprint repeated")
    monkeypatch.setattr("jobsearch_agent.loop.LiveApplicationOrchestrator", lambda *a, **k: _FakeOrchestrator(live))

    result = ApplicationLoop(database, _runtime(live=live)).run(job.id, submit=True)

    assert result.status == "LOOP_DETECTED"
    assert result.terminal is True
    # `requires_action` pode nomear uma ACAO, e nao apenas um estado: aqui o que
    # falta nao e uma resposta, e sim corrigir o fluxo.
    assert result.requires_action == "LOOP_DETECTED"
    database.close()


def test_without_submit_the_loop_stops_at_the_review_surface(tmp_path: Path, monkeypatch, monkeypatch_coordinator=None):
    database = Database(tmp_path / "loop.db")
    service = ApplicationService(database)
    job = _job(database)
    service.create_for_job(job.id)

    form = ApplicationForm(
        form_id="form-loop",
        provider="greenhouse",
        fields=[ApplicationField(key="job_application[first_name]", label="First name", required=True, value="Loop")],
    )
    live = LiveApplicationResult(
        "FILLED_REVIEW_REQUIRED",
        provider="greenhouse",
        url=job.url,
        cycles=[DryRunCycle(1, "fingerprint", ApplicationState.READY_TO_APPLY)],
        form=form,
        form_fingerprint="fingerprint",
    )
    monkeypatch.setattr("jobsearch_agent.loop.LiveApplicationOrchestrator", lambda *a, **k: _FakeOrchestrator(live))

    loop = ApplicationLoop(database, _runtime(live=live), coordinator=_ExplodingCoordinator())
    result = loop.run(job.id, submit=False)

    assert result.status == "READY_TO_SUBMIT"
    assert result.terminal is False
    assert result.requires_action == "submit"
    assert result.submission_attempted is False
    assert result.form_fingerprint == "fingerprint"
    assert result.answers_fingerprint, "o fingerprint das respostas precede qualquer envio"
    assert database.get_application(result.application_id).state is ApplicationState.REVIEW_REACHED
    database.close()


def test_the_result_serializes_for_the_cli(tmp_path: Path):
    database = Database(tmp_path / "loop.db")
    service = ApplicationService(database)
    job = _job(database)
    application = service.create_for_job(job.id)
    _to_submitted(service, application.id)

    result = ApplicationLoop(database, _forbidden_runtime("nao deveria executar")).run(job.id)
    payload = result.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["status"] == "ALREADY_SUBMITTED"
    assert payload["state"] in {state.value for state in ApplicationState}
    database.close()


def test_the_state_sets_express_the_right_relations():
    """Terminal, recuperavel e "nunca reenviar" sao coisas diferentes."""
    assert TERMINAL_STATES & RECOVERABLE_STATES == set()
    assert {ApplicationState.SUBMITTED, ApplicationState.SUBMIT_UNKNOWN} <= NO_RESEND_STATES
    # AWAITING nunca e reenviada, mas continua recuperavel: o desfecho ainda
    # pode vir de evidencia independente. Por isso NO_RESEND nao esta contido em
    # TERMINAL.
    assert ApplicationState.AWAITING_SUBMISSION_CONFIRMATION in NO_RESEND_STATES
    assert ApplicationState.AWAITING_SUBMISSION_CONFIRMATION not in TERMINAL_STATES
    assert ApplicationState.AWAITING_SUBMISSION_CONFIRMATION in RECOVERABLE_STATES


class _FakeOrchestrator:
    def __init__(self, live: LiveApplicationResult) -> None:
        self.live = live

    def run(self, session, context, url, audit_dir=None):  # noqa: ANN001 - assinatura do orquestrador
        context.form = self.live.form
        return self.live


class _ExplodingCoordinator:
    def submit(self, **_kwargs):
        raise AssertionError("sem --submit o loop nao pode submeter")
