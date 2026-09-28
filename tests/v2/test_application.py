from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from job_agent_v2.application import Application, ApplicationStateError
from job_agent_v2.models import State


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


def test_application_authorization_binds_three_fingerprints_and_is_one_shot():
    app = Application("app-1").transition(State.READY).transition(State.FILLING).transition(State.FILLED).transition(State.READY_TO_SUBMIT)
    app = app.with_review(form={"fields": ["email"]}, answers={"email": "ok"}, resume_sha256="resume-sha")
    app = app.authorize(expires_at=_expires())
    assert app.authorization is not None
    consumed = app.authorization.consume(
        application_id=app.id,
        form_fingerprint=app.form_fingerprint,
        answers_fingerprint=app.answers_fingerprint,
        resume_sha256=app.resume_sha256,
    )
    assert consumed.used is True
    with pytest.raises(ApplicationStateError, match="already used"):
        consumed.consume(
            application_id=app.id,
            form_fingerprint=app.form_fingerprint,
            answers_fingerprint=app.answers_fingerprint,
            resume_sha256=app.resume_sha256,
        )


def test_application_rejects_fingerprint_drift_and_invalid_transition():
    app = Application("app-1").transition(State.READY).transition(State.FILLING).transition(State.FILLED).transition(State.READY_TO_SUBMIT)
    app = app.with_review(form={"fields": ["email"]}, answers={"email": "ok"}, resume_sha256="resume-sha").authorize(expires_at=_expires())
    with pytest.raises(ApplicationStateError, match="fingerprints"):
        app.authorization.consume(
            application_id=app.id,
            form_fingerprint="changed",
            answers_fingerprint=app.answers_fingerprint,
            resume_sha256=app.resume_sha256,
        )
    with pytest.raises(ApplicationStateError, match="invalid transition"):
        Application("app-2").transition(State.SUBMITTED)
