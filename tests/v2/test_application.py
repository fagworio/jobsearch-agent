from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from job_agent_v2.application import Application, ApplicationStateError, greenhouse_form_fingerprint
from job_agent_v2.challenges import ChallengeState
from job_agent_v2.confirmation import ConfirmationState, classify_browser_result
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


def test_application_can_pause_for_human_and_resume_after_clear_challenge():
    app = Application("app-1").transition(State.READY).transition(State.FILLING).transition(State.FILLED)
    app = app.observe_challenge(ChallengeState.BLOCKING).transition(State.WAITING_HUMAN)
    app = app.observe_challenge(ChallengeState.CLEAR).transition(State.READY_TO_SUBMIT)
    assert app.state is State.READY_TO_SUBMIT


def test_confirmation_requires_explicit_primary_evidence():
    unknown = classify_browser_result({"primary": {"url": "https://example.test"}})
    assert unknown.state is ConfirmationState.SUBMIT_UNKNOWN
    submitted = classify_browser_result({"primary": {"url": "https://example.test/confirmation", "confirmation_component": True}})
    assert submitted.state is ConfirmationState.SUBMITTED


def test_begin_submit_consumes_authorization_only_with_clear_challenge():
    app = Application("app-1").transition(State.READY).transition(State.FILLING).transition(State.FILLED)
    app = app.observe_challenge(ChallengeState.CLEAR).transition(State.READY_TO_SUBMIT)
    app = app.with_review(form={"fields": ["email"]}, answers={"email": "ok"}, resume_sha256="resume-sha")
    app = app.authorize(expires_at=_expires())
    submitting = app.begin_submit()
    assert submitting.state is State.SUBMITTING
    assert submitting.authorization is not None and submitting.authorization.used is True


def test_greenhouse_form_fingerprint_ignores_current_values():
    before = {"fields": [{"id": "email", "type": "email", "label": "Email", "required": True, "options": [], "value": ""}]}
    after = {"fields": [{"id": "email", "type": "email", "label": "Email", "required": True, "options": [], "value": "user@example.test"}]}
    assert greenhouse_form_fingerprint(before) == greenhouse_form_fingerprint(after)
