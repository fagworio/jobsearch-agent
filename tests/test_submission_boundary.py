from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationDomainError, ApplicationService
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.submission import (
    LiveNetworkPolicy,
    SubmissionBoundaryError,
    SubmissionService,
    SubmissionVerification,
    build_review_snapshot,
)


def _ready_application(db: Database) -> str:
    job = Job(
        id="job-submission",
        source="greenhouse",
        external_id="123",
        company="Acme",
        title="Engineer",
        description="Build software",
    )
    db.save_job(job, "greenhouse:123", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "safety_gate_evaluated")
    return application.id


def _intent(service: SubmissionService, application_id: str):
    return service.create_intent(
        application_id=application_id,
        job_id="job-submission",
        provider="greenhouse",
        destination="https://boards.greenhouse.io/acme/jobs/123",
        form_fingerprint="form-v1",
        resume_sha256="resume-v1",
        answers_fingerprint="answers-v1",
        expires_in_seconds=300,
    )


def test_submission_intent_is_bound_to_review_snapshot_and_requires_authorization(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    application_id = _ready_application(db)
    service = SubmissionService(db)
    snapshot = build_review_snapshot(
        application_id=application_id,
        job_id="job-submission",
        company="Acme",
        title="Engineer",
        provider="greenhouse",
        destination="https://boards.greenhouse.io/acme/jobs/123",
        resume_filename="resume.pdf",
        resume_sha256="resume-v1",
        resolved_fields=[{"key": "name", "value": "Candidate"}],
        manual_questions=[{"key": "sponsorship", "status": "approved"}],
    )
    intent = _intent(service, application_id)
    assert intent.form_fingerprint == "form-v1"
    assert snapshot.resume_sha256 == intent.resume_sha256
    with pytest.raises(SubmissionBoundaryError, match="authorization"):
        service.begin_submission(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=LiveNetworkPolicy.for_submission("greenhouse", application_id, intent.id),
            method="POST",
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
    authorized = service.authorize_submission(intent.id)
    assert authorized.status == "AUTHORIZED"
    assert db.get_application(application_id).state == ApplicationState.SUBMIT_AUTHORIZED
    db.close()


def test_authorization_must_bind_form_resume_and_answers_before_write(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    application_id = _ready_application(db)
    service = SubmissionService(db)
    intent = _intent(service, application_id)
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy.for_submission("greenhouse", application_id, intent.id)
    for kwargs in (
        {"current_form_fingerprint": "changed-form", "current_resume_sha256": "resume-v1", "current_answers_fingerprint": "answers-v1"},
        {"current_form_fingerprint": "form-v1", "current_resume_sha256": "changed-resume", "current_answers_fingerprint": "answers-v1"},
        {"current_form_fingerprint": "form-v1", "current_resume_sha256": "resume-v1", "current_answers_fingerprint": "changed-answers"},
    ):
        with pytest.raises(SubmissionBoundaryError, match="fingerprint|SHA256"):
            service.begin_submission(
                intent.id,
                **kwargs,
                policy=policy,
                method="POST",
                url="https://boards.greenhouse.io/acme/jobs/123",
            )
    assert db.get_application(application_id).state == ApplicationState.SUBMIT_AUTHORIZED
    db.close()


def test_live_network_policy_rejects_unexpected_origin_path_method_and_stage():
    policy = LiveNetworkPolicy.for_submission("greenhouse", "application-1", "intent-1")
    assert policy.validate("POST", "https://boards.greenhouse.io/acme/jobs/123", "SUBMIT").valid
    for method, url, stage in (
        ("GET", "https://boards.greenhouse.io/acme/jobs/123", "SUBMIT"),
        ("POST", "https://evil.example/acme/jobs/123", "SUBMIT"),
        ("POST", "https://boards.greenhouse.io/acme/other/123", "SUBMIT"),
        ("POST", "https://boards.greenhouse.io/acme/jobs/123", "INTERMEDIATE_WRITE"),
    ):
        assert not policy.validate(method, url, stage).valid


def test_confirmed_submission_is_verified_and_duplicate_is_blocked(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    application_id = _ready_application(db)
    service = SubmissionService(db)
    intent = _intent(service, application_id)
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy.for_submission("greenhouse", application_id, intent.id)
    attempt = service.begin_submission(
        intent.id,
        current_form_fingerprint="form-v1",
        current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1",
        policy=policy,
        method="POST",
        url="https://boards.greenhouse.io/acme/jobs/123",
    )
    assert db.get_application(application_id).state == ApplicationState.SUBMITTING
    result = service.record_result(
        attempt.id,
        SubmissionVerification.confirmed("ui_confirmation", {"message": "Application submitted"}),
    )
    assert result.status == "SUBMITTED"
    assert db.get_application(application_id).state == ApplicationState.SUBMITTED
    with pytest.raises(SubmissionBoundaryError, match="duplicate"):
        service.begin_submission(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            method="POST",
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
    db.close()


def test_timeout_after_request_becomes_unknown_and_cannot_retry(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    application_id = _ready_application(db)
    service = SubmissionService(db)
    intent = _intent(service, application_id)
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy.for_submission("greenhouse", application_id, intent.id)
    attempt = service.begin_submission(
        intent.id,
        current_form_fingerprint="form-v1",
        current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1",
        policy=policy,
        method="POST",
        url="https://boards.greenhouse.io/acme/jobs/123",
    )
    result = service.record_result(attempt.id, SubmissionVerification.unknown("timeout"))
    assert result.status == "SUBMIT_UNKNOWN"
    assert db.get_application(application_id).state == ApplicationState.SUBMIT_UNKNOWN
    with pytest.raises(SubmissionBoundaryError, match="unknown"):
        service.begin_submission(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            method="POST",
            url="https://boards.greenhouse.io/acme/jobs/123",
        )
    db.close()


def test_submission_result_evidence_is_redacted(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    application_id = _ready_application(db)
    service = SubmissionService(db)
    intent = _intent(service, application_id)
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy.for_submission("greenhouse", application_id, intent.id)
    attempt = service.begin_submission(
        intent.id,
        current_form_fingerprint="form-v1",
        current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1",
        policy=policy,
        method="POST",
        url="https://boards.greenhouse.io/acme/jobs/123",
    )
    service.record_result(
        attempt.id,
        SubmissionVerification.confirmed(
            "provider_response",
            {"status_code": 201, "body": "candidate@example.com and full response body"},
        ),
    )
    saved = db.list_submission_attempts(application_id)[0]
    assert "body" not in saved.evidence
    assert "candidate@example.com" not in str(saved.evidence)
    db.close()


def test_submission_state_cannot_be_authorized_from_wrong_application_state(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    job = Job(id="job-draft", source="greenhouse", external_id="draft", company="Acme", title="Engineer", description="Build")
    db.save_job(job, "greenhouse:draft", {})
    application_id = ApplicationService(db).create_for_job(job.id).id
    service = SubmissionService(db)
    intent = service.create_intent(
        application_id=application_id,
        job_id=job.id,
        provider="greenhouse",
        destination="https://boards.greenhouse.io/acme/jobs/draft",
        form_fingerprint="form",
        resume_sha256="resume",
        answers_fingerprint="answers",
        expires_in_seconds=300,
        require_ready=False,
    )
    with pytest.raises(ApplicationDomainError, match="READY_TO_APPLY"):
        service.authorize_submission(intent.id)
    db.close()
