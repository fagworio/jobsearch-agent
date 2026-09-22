from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.submission import LiveNetworkPolicy, SubmissionService, SubmissionVerification, build_review_snapshot


def test_submission_evidence_redacts_untrusted_tokens_and_redirect_queries(tmp_path: Path):
    db = Database(tmp_path / "submission-redaction.db")
    job = Job(id="job-redaction", source="greenhouse", external_id="redaction", company="Acme", title="Engineer", description="Build")
    db.save_job(job, "greenhouse:redaction", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    application = service.transition(application.id, ApplicationState.PREPARING, "prepare")
    application = service.transition(application.id, ApplicationState.MATERIALS_READY, "materials")
    application = service.transition(application.id, ApplicationState.READY_TO_APPLY, "ready")
    submission = SubmissionService(db)
    intent = submission.create_intent(
        application_id=application.id,
        job_id=job.id,
        provider="greenhouse",
        destination="https://boards.greenhouse.io/acme/jobs/redaction",
        form_fingerprint="form",
        resume_sha256="resume",
        answers_fingerprint="answers",
        expires_in_seconds=300,
    )
    submission.save_review_snapshot(
        build_review_snapshot(
            application_id=application.id,
            job_id=job.id,
            company="Acme",
            title="Engineer",
            provider="greenhouse",
            destination=intent.destination,
            resume_filename="resume.pdf",
            resume_sha256=intent.resume_sha256,
        )
    )
    submission.authorize_submission(intent.id)
    policy = LiveNetworkPolicy.for_submission("greenhouse", application.id, intent.id)
    attempt = submission.begin_submission(
        intent.id,
        current_form_fingerprint="form",
        current_resume_sha256="resume",
        current_answers_fingerprint="answers",
        policy=policy,
        method="POST",
        url=intent.destination,
    )
    submission.record_result(
        attempt.id,
        SubmissionVerification.confirmed(
            "provider response for candidate@example.com",
            {
                "status_code": "201",
                "provider_status": "accepted for candidate@example.com",
                "redirect_path": "/confirmation?email=candidate@example.com",
                "body": "full PII response",
            },
        ),
    )
    saved = db.list_submission_attempts(application.id)[0]
    assert "candidate@example.com" not in str(saved.evidence)
    assert "body" not in saved.evidence
    assert "redirect_path" not in saved.evidence
    assert "redirect_path_hash" not in saved.evidence
    assert saved.evidence == {
        "confirmation_type": "redacted",
        "provider_status": "redacted",
    }
    db.close()
