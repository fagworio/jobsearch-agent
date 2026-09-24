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
    CONFIRMATION_CONFIDENCE_FLOOR,
    ConfirmationSource,
    Job,
    REJECTED_EVIDENCE_KINDS,
    SubmissionConfirmationEvidence,
)
from jobsearch_agent.persistence import Database

OBSERVED_AT = "2026-09-24T16:00:00+00:00"


def _evidence(
    source: ConfirmationSource = ConfirmationSource.CONFIRMATION_EMAIL,
    *,
    reference: str = "msg-abc123",
    provider: str = "gmail",
    confidence: float = 1.0,
    observed_at: str = OBSERVED_AT,
) -> SubmissionConfirmationEvidence:
    return SubmissionConfirmationEvidence(
        source=source,
        observed_at=observed_at,
        reference=reference,
        provider=provider,
        confidence=confidence,
    )


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
    evidence = _evidence()
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
    evidence = _evidence(ConfirmationSource.PROVIDER_APPLICATION_STATUS)
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
        SubmissionConfirmationEvidence(
            source="o usuario disse", observed_at=OBSERVED_AT, reference="x", provider="gmail"
        )
    with pytest.raises(ValueError):
        SubmissionConfirmationEvidence(
            source=ConfirmationSource.CONFIRMATION_EMAIL,
            observed_at=OBSERVED_AT,
            reference="Assunto: sua candidatura foi recebida",
            provider="gmail",
        )


# --- JSA-CG-018: o que NAO e evidencia ----------------------------------------


def test_a_declaration_is_never_construable_as_evidence():
    """`user_report`, `manual_checkbox`, `free_text`, `handoff_completion`.

    Sao declaracoes do interessado, nao observacoes de terceiro. Nao entram no
    conjunto aceito — a lista existe como dado para que a recusa seja nomeada.
    """
    accepted = {source.value for source in ConfirmationSource}
    for kind in REJECTED_EVIDENCE_KINDS:
        assert kind not in accepted, f"{kind} entrou no conjunto de fontes aceitas"
        with pytest.raises(ValueError, match="unsupported confirmation source"):
            SubmissionConfirmationEvidence(
                source=kind, observed_at=OBSERVED_AT, reference="x", provider="gmail"
            )


def test_evidence_without_a_traceable_reference_is_refused():
    for reference in ("", "Assunto: sua candidatura foi recebida", "a" * 80):
        with pytest.raises(ValueError, match="reference"):
            _evidence(reference=reference)


def test_evidence_without_a_provider_or_timestamp_is_refused():
    with pytest.raises(ValueError, match="provider"):
        _evidence(provider="")
    with pytest.raises(ValueError, match="timezone"):
        _evidence(observed_at="2026-09-24T16:00:00")
    with pytest.raises(ValueError, match="ISO-8601"):
        _evidence(observed_at="ontem")
    with pytest.raises(ValueError, match="future"):
        _evidence(observed_at="2099-01-01T00:00:00+00:00")


def test_evidence_below_the_confidence_floor_is_refused(tmp_path):
    service, application_id = _awaiting(tmp_path, "weak")
    weak = _evidence(confidence=CONFIRMATION_CONFIDENCE_FLOOR - 0.1)
    with pytest.raises(ApplicationDomainError, match="too weak"):
        service.confirm_submission(application_id, weak)
    assert service.database.get_application(application_id).state is (
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION
    )
    service.database.close()


def test_the_transition_door_validates_the_evidence_too(tmp_path):
    """A regra nao pode depender de qual comando chamou `transition`."""
    service, application_id = _awaiting(tmp_path, "door")
    weak = _evidence(confidence=0.1)
    with pytest.raises(ApplicationDomainError, match="too weak"):
        service.transition(
            application_id,
            ApplicationState.SUBMITTED,
            "qualquer_coisa",
            confirmation_evidence=weak,
        )
    assert service.database.get_application(application_id).state is (
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION
    )
    service.database.close()


def test_the_confirmation_event_records_what_was_observed(tmp_path):
    service, application_id = _awaiting(tmp_path, "payload")
    service.confirm_submission(
        application_id,
        _evidence(ConfirmationSource.PROVIDER_APPLICATION_STATUS, reference="status-9911", provider="lever"),
    )
    event = service.database.list_application_events(application_id)[-1]
    assert event.event == "submission_confirmed"
    assert event.payload == {
        "previous_state": ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value,
        "source": "provider_application_status",
        "reference": "status-9911",
        "provider": "lever",
        "confidence": 1.0,
        "observed_at": OBSERVED_AT,
        "signals": [],
    }
    service.database.close()


def test_every_state_has_a_transition_entry():
    for state in ApplicationState:
        assert state in TRANSITIONS, f"{state.value} sem entrada na maquina"
