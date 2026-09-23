from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.greenhouse import GreenhouseSubmissionExecutor
from jobsearch_agent.submission import (
    LiveNetworkPolicy,
    SubmissionBoundaryError,
    SubmissionService,
    build_review_snapshot,
)
from tests.submission_server import SubmissionTestServer


def _ready_application(db: Database, suffix: str) -> str:
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
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "safety_gate_evaluated")
    return application.id


def _intent(db: Database, application_id: str, destination: str, path: str):
    service = SubmissionService(db)
    intent = service.create_intent(
        application_id=application_id,
        job_id=db.get_application(application_id).job_id,
        provider="greenhouse",
        destination=destination,
        form_fingerprint="form-v1",
        resume_sha256="resume-v1",
        answers_fingerprint="answers-v1",
        expires_in_seconds=300,
        allow_insecure_destination=True,
    )
    service.save_review_snapshot(
        build_review_snapshot(
            application_id=application_id,
            job_id=intent.job_id,
            company="Acme",
            title="Engineer",
            provider="greenhouse",
            destination=destination,
            resume_filename="resume.pdf",
            resume_sha256=intent.resume_sha256,
            form_fingerprint=intent.form_fingerprint,
            answers_fingerprint=intent.answers_fingerprint,
        )
    )
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy(
        provider="greenhouse",
        allowed_origin=destination.split(path)[0],
        allowed_path_pattern=rf"^{path}$",
        allowed_method="POST",
        allowed_stage="SUBMIT",
        application_id=application_id,
        submission_intent_id=intent.id,
    )
    return intent, policy


def test_greenhouse_executor_posts_once_and_requires_provider_confirmation(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "success")
        destination = server.url("/submit/success")
        intent, policy = _intent(db, application_id, destination, "/submit/success")
        result = GreenhouseSubmissionExecutor(db, timeout=1.0).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            payload={"name": "Candidate", "email": "candidate@example.test"},
        )
        assert result.status == "SUBMITTED"
        assert result.http_status == 201
        assert len(server.requests) == 1
        assert db.get_application(application_id).state == ApplicationState.SUBMITTED
        db.close()


def test_greenhouse_validation_error_becomes_submit_failed(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "validation")
        intent, policy = _intent(db, application_id, server.url("/submit/validation"), "/submit/validation")
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            payload={"name": "Candidate"},
        )
        assert result.status == "SUBMIT_FAILED"
        assert result.http_status == 422
        assert db.get_application(application_id).state == ApplicationState.SUBMIT_FAILED
        db.close()


@pytest.mark.parametrize("path,expected_status", [("/submit/error", "SUBMIT_FAILED"), ("/submit/redirect", "SUBMIT_UNKNOWN")])
def test_server_error_or_redirect_never_becomes_confirmed(tmp_path: Path, path: str, expected_status: str):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        suffix = path.rsplit("/", 1)[-1]
        application_id = _ready_application(db, suffix)
        intent, policy = _intent(db, application_id, server.url(path), path)
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            payload={"name": "Candidate"},
        )
        assert result.status == expected_status
        assert result.status != "SUBMITTED"
        db.close()


def test_timeout_becomes_submit_unknown_and_is_not_retried(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "timeout")
        intent, policy = _intent(db, application_id, server.url("/submit/timeout"), "/submit/timeout")
        executor = GreenhouseSubmissionExecutor(db, timeout=0.05)
        result = executor.submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            payload={"name": "Candidate"},
        )
        assert result.status == "SUBMIT_UNKNOWN"
        with pytest.raises(SubmissionBoundaryError, match="unknown"):
            executor.submit(
                intent.id,
                current_form_fingerprint="form-v1",
                current_resume_sha256="resume-v1",
                current_answers_fingerprint="answers-v1",
                policy=policy,
                payload={"name": "Candidate"},
            )
        db.close()


def test_duplicate_request_is_blocked_before_second_network_write(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "duplicate")
        intent, policy = _intent(db, application_id, server.url("/submit/duplicate"), "/submit/duplicate")
        executor = GreenhouseSubmissionExecutor(db)
        first = executor.submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            payload={"name": "Candidate"},
        )
        assert first.status == "SUBMITTED"
        with pytest.raises(SubmissionBoundaryError, match="duplicate"):
            executor.submit(
                intent.id,
                current_form_fingerprint="form-v1",
                current_resume_sha256="resume-v1",
                current_answers_fingerprint="answers-v1",
                policy=policy,
                payload={"name": "Candidate"},
            )
        assert len(server.requests) == 1
        db.close()


def test_unexpected_endpoint_is_blocked_without_request(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "unexpected")
        intent, policy = _intent(db, application_id, server.url("/submit/success"), "/submit/success")
        bad_policy = LiveNetworkPolicy(
            provider="greenhouse",
            allowed_origin=server.origin,
            allowed_path_pattern=r"^/submit/other$",
            allowed_method="POST",
            allowed_stage="SUBMIT",
            application_id=application_id,
            submission_intent_id=intent.id,
        )
        with pytest.raises(SubmissionBoundaryError, match="path"):
            GreenhouseSubmissionExecutor(db).submit(
                intent.id,
                current_form_fingerprint="form-v1",
                current_resume_sha256="resume-v1",
                current_answers_fingerprint="answers-v1",
                policy=bad_policy,
                payload={"name": "Candidate"},
            )
        assert not server.requests
        db.close()
