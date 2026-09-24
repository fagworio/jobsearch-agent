"""ADR 0005: handoff humano e confirmacao manual de submissao.

Os testes que importam sao os NEGATIVOS. A maquina de estados e o contrato; o
CLI, quando existir, apenas chama operacoes que ela ja restringe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationDomainError, ApplicationService, TRANSITIONS
from jobsearch_agent.models import (
    ApplicationState,
    ConfirmationSource,
    Job,
    SubmissionConfirmationEvidence,
)
from jobsearch_agent.persistence import Database


def _service(tmp_path: Path, suffix: str) -> tuple[ApplicationService, str]:
    db = Database(tmp_path / f"{suffix}.db")
    job = Job(
        id=f"job-{suffix}",
        source="greenhouse",
        external_id=suffix,
        company="Acme",
        title="Engineer",
        description="Build software",
    )
    db.save_job(job, f"greenhouse:{suffix}", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    return service, application.id


def _advance(service: ApplicationService, application_id: str, *states: ApplicationState) -> None:
    for state in states:
        service.transition(application_id, state, f"to_{state.value.casefold()}")


def _in_handoff(tmp_path: Path, suffix: str) -> tuple[ApplicationService, str]:
    """Chega a HANDOFF_IN_PROGRESS pelo caminho real, sem atalho."""
    service, application_id = _service(tmp_path, suffix)
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
    return service, application_id


def _awaiting(tmp_path: Path, suffix: str) -> tuple[ApplicationService, str]:
    service, application_id = _in_handoff(tmp_path, suffix)
    service.report_manual_submission(application_id)
    return service, application_id


# --- o caminho feliz ----------------------------------------------------------


def test_handoff_starts_only_from_a_provider_rejection(tmp_path):
    service, application_id = _service(tmp_path, "start")
    _advance(
        service, application_id,
        ApplicationState.PREPARING, ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED, ApplicationState.SUBMITTING,
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
    )
    moved = service.start_human_handoff(application_id)
    assert moved.state is ApplicationState.HANDOFF_IN_PROGRESS
    assert service.database.get_application(application_id).state is ApplicationState.HANDOFF_IN_PROGRESS
    service.database.close()


def test_cancelling_before_any_report_returns_to_review(tmp_path):
    service, application_id = _in_handoff(tmp_path, "cancel")
    assert service.cancel_handoff(application_id).state is ApplicationState.REVIEW_REACHED
    service.database.close()


def test_reporting_a_manual_submission_does_not_mark_submitted(tmp_path):
    """O relato cria a incerteza; nao pode ser o que a satisfaz."""
    service, application_id = _awaiting(tmp_path, "report")
    stored = service.database.get_application(application_id)
    assert stored.state is ApplicationState.AWAITING_SUBMISSION_CONFIRMATION
    assert stored.state is not ApplicationState.SUBMITTED
    service.database.close()


def test_independent_evidence_is_what_reaches_submitted(tmp_path):
    service, application_id = _awaiting(tmp_path, "confirm")
    evidence = SubmissionConfirmationEvidence(
        source=ConfirmationSource.CONFIRMATION_EMAIL, reference="msg-abc123"
    )
    assert service.confirm_submission(application_id, evidence).state is ApplicationState.SUBMITTED
    service.database.close()


def test_the_explicit_retry_path_survives(tmp_path):
    """O caminho manual e ADITIVO: retry-submit continua valendo."""
    service, application_id = _service(tmp_path, "retry-keep")
    _advance(
        service, application_id,
        ApplicationState.PREPARING, ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED, ApplicationState.SUBMITTING,
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
    )
    assert ApplicationState.REVIEW_REACHED in TRANSITIONS[ApplicationState.NEEDS_HUMAN_CAPTCHA]
    service.database.close()


# --- as invariantes: negativos ------------------------------------------------


def test_retry_submit_is_blocked_while_the_handoff_is_in_progress(tmp_path):
    service, application_id = _in_handoff(tmp_path, "retry-handoff")
    with pytest.raises(ApplicationDomainError, match="submit retry requires"):
        service.retry_submit(application_id)
    service.database.close()


def test_retry_submit_is_blocked_after_the_manual_report(tmp_path):
    """Reabrir o submit depois do relato violaria exactly-once."""
    service, application_id = _awaiting(tmp_path, "retry-awaiting")
    with pytest.raises(ApplicationDomainError, match="submit retry requires"):
        service.retry_submit(application_id)
    service.database.close()


def test_authorize_submit_is_blocked_after_the_manual_report(tmp_path):
    """Existe a possibilidade real de ja ter sido enviada: nao autorizar de novo."""
    from jobsearch_agent.submission import SubmissionBoundaryError, SubmissionService

    service, application_id = _awaiting(tmp_path, "authorize-blocked")
    submission = SubmissionService(service.database)
    intent = submission.create_intent(
        application_id=application_id,
        job_id=service.database.get_application(application_id).job_id,
        provider="greenhouse",
        destination="https://boards.greenhouse.io/acme/jobs/1",
        form_fingerprint="f" * 64,
        resume_sha256="a" * 64,
        answers_fingerprint="b" * 64,
        expires_in_seconds=300,
        require_ready=False,
    )
    with pytest.raises((SubmissionBoundaryError, ApplicationDomainError), match="requires READY_TO_APPLY"):
        submission.authorize_submission(intent.id)
    assert service.database.get_application(application_id).state is (
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION
    )
    service.database.close()


def test_the_forbidden_edges_do_not_exist(tmp_path):
    """A ausencia da aresta e a regra — nao uma checagem em runtime."""
    allowed = TRANSITIONS[ApplicationState.AWAITING_SUBMISSION_CONFIRMATION]
    for forbidden in (
        ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED,
        ApplicationState.SUBMITTING,
        ApplicationState.SUBMIT_FAILED,
        ApplicationState.SUBMIT_UNKNOWN,
    ):
        assert forbidden not in allowed, f"aresta proibida: AWAITING -> {forbidden.value}"
    assert allowed == {ApplicationState.SUBMITTED}


def test_cancelling_after_the_report_has_no_path_back(tmp_path):
    service, application_id = _awaiting(tmp_path, "cancel-after")
    with pytest.raises(ApplicationDomainError, match="cancel handoff requires"):
        service.cancel_handoff(application_id)
    service.database.close()


def test_a_generic_transition_cannot_reach_submitted_without_evidence(tmp_path):
    """Invariante 1: nenhum comando baseado SO em declaracao leva a SUBMITTED."""
    service, application_id = _awaiting(tmp_path, "no-evidence")
    with pytest.raises(ApplicationDomainError, match="independent confirmation evidence"):
        service.transition(application_id, ApplicationState.SUBMITTED, "user_says_so")
    assert service.database.get_application(application_id).state is (
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION
    )
    service.database.close()


def test_confirming_from_the_wrong_state_is_refused(tmp_path):
    service, application_id = _in_handoff(tmp_path, "confirm-wrong")
    evidence = SubmissionConfirmationEvidence(source=ConfirmationSource.PROVIDER_APPLICATION_STATUS)
    with pytest.raises(ApplicationDomainError, match="confirmation requires"):
        service.confirm_submission(application_id, evidence)
    service.database.close()


def test_reporting_from_the_wrong_state_is_refused(tmp_path):
    service, application_id = _service(tmp_path, "report-wrong")
    with pytest.raises(ApplicationDomainError, match="manual submission report requires"):
        service.report_manual_submission(application_id)
    service.database.close()


def test_resume_does_not_put_a_handoff_back_on_the_automatic_flow(tmp_path):
    """`HANDOFF_IN_PROGRESS` e retomavel so no sentido de continuar o handoff."""
    service, application_id = _in_handoff(tmp_path, "resume")
    with pytest.raises(ApplicationDomainError, match="cannot be resumed"):
        service.resume(application_id)
    service.database.close()


def test_a_handoff_started_twice_is_refused(tmp_path):
    service, application_id = _in_handoff(tmp_path, "twice")
    with pytest.raises(ApplicationDomainError, match="handoff requires NEEDS_HUMAN_CAPTCHA"):
        service.start_human_handoff(application_id)
    service.database.close()


def test_an_invalid_evidence_source_is_refused(tmp_path):
    with pytest.raises(ValueError):
        SubmissionConfirmationEvidence(source="o usuario disse", reference="x")
    with pytest.raises(ValueError):
        SubmissionConfirmationEvidence(
            source=ConfirmationSource.CONFIRMATION_EMAIL,
            reference="Assunto: sua candidatura foi recebida",
        )


def test_every_state_has_a_transition_entry():
    for state in ApplicationState:
        assert state in TRANSITIONS, f"{state.value} sem entrada na maquina"
