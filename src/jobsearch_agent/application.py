"""Application domain, state transitions and Safety Gate.

This module has no browser or ATS dependency. It is intentionally usable by a
CLI, Hermes or a future Playwright adapter.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import Application, ApplicationContext, ApplicationEvent, ApplicationPolicy, ApplicationReadiness, ApplicationState
from .persistence import Database


class ApplicationDomainError(ValueError):
    pass


TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.DRAFT: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.PREPARING: {ApplicationState.MATERIALS_READY, ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_LOGIN, ApplicationState.NEEDS_MFA, ApplicationState.NEEDS_CAPTCHA, ApplicationState.UNSUPPORTED_FORM, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.MATERIALS_READY: {ApplicationState.READY_FOR_REVIEW, ApplicationState.READY_TO_APPLY, ApplicationState.NEEDS_ANSWER, ApplicationState.UNSUPPORTED_FORM, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.READY_FOR_REVIEW: {ApplicationState.READY_TO_APPLY, ApplicationState.NEEDS_ANSWER, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.READY_TO_APPLY: set(),
    ApplicationState.NEEDS_ANSWER: {ApplicationState.PREPARING, ApplicationState.MATERIALS_READY, ApplicationState.READY_FOR_REVIEW, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.NEEDS_LOGIN: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED},
    ApplicationState.NEEDS_MFA: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED},
    ApplicationState.NEEDS_CAPTCHA: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED},
    ApplicationState.UNSUPPORTED_FORM: {ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.POLICY_BLOCKED: {ApplicationState.PREPARING, ApplicationState.REJECTED},
    ApplicationState.REJECTED: set(),
}


def application_id_for_job(job_id: str) -> str:
    return "application-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]


def load_application_policy(path: str | Path) -> ApplicationPolicy:
    source = Path(path)
    if not source.exists():
        return ApplicationPolicy()
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    autonomy = raw.get("autonomy", {}) if isinstance(raw, dict) else {}
    limits = raw.get("limits", {}) if isinstance(raw, dict) else {}
    safety = raw.get("safety", {}) if isinstance(raw, dict) else {}
    policy = ApplicationPolicy()
    policy.autonomy.update({str(key): str(value) for key, value in autonomy.items()})
    policy.applications_per_day = int(limits.get("applications_per_day", policy.applications_per_day))
    policy.unknown_answer = str(safety.get("unknown_answer", policy.unknown_answer))
    policy.captcha = str(safety.get("captcha", policy.captcha))
    policy.mfa = str(safety.get("mfa", policy.mfa))
    policy.legal_question = str(safety.get("legal_question", policy.legal_question))
    return policy


class ApplicationService:
    def __init__(self, database: Database):
        self.database = database

    def create_for_job(self, job_id: str) -> Application:
        existing = self.database.get_application_for_job(job_id)
        if existing:
            return existing
        if not self.database.get_job(job_id):
            raise ApplicationDomainError(f"job not found: {job_id}")
        application = Application(application_id_for_job(job_id), job_id)
        self.database.save_application(application)
        return application

    def transition(self, application_id: str, target: ApplicationState, event: str, payload: dict[str, Any] | None = None, expected_state: ApplicationState | None = None) -> Application:
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        previous_state = expected_state or application.state
        if target not in TRANSITIONS[previous_state]:
            raise ApplicationDomainError(f"invalid application transition: {previous_state.value} -> {target.value}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        application.state = target
        application.updated_at = now
        self.database.save_application_transition(application, ApplicationEvent(application.id, previous_state, target, event, payload or {}, now))
        return application

    def resume(self, application_id: str) -> Application:
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        resumable = {ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_LOGIN, ApplicationState.NEEDS_MFA, ApplicationState.NEEDS_CAPTCHA}
        if application.state not in resumable:
            raise ApplicationDomainError(f"application state cannot be resumed: {application.state.value}")
        return self.transition(application_id, ApplicationState.PREPARING, "application_resumed", {"previous_state": application.state.value})


def evaluate_safety_gate(context: ApplicationContext, required_fields: list[Any] | None = None) -> ApplicationReadiness:
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    all_fit_blockers = list(context.fit.get("blockers", []))
    unknown_fit = [item for item in all_fit_blockers if item == "work_authorization_unknown"]
    fit_blockers = [item for item in all_fit_blockers if item not in unknown_fit]
    checks.append({"gate": "fit", "result": "blocker" if fit_blockers else "pass", "evidence": fit_blockers or context.fit.get("criteria", [])})
    if fit_blockers:
        blockers.extend(f"fit:{item}" for item in fit_blockers)
    if unknown_fit:
        checks.append({"gate": "work_authorization", "result": "unknown", "evidence": unknown_fit})
        blockers.append("unknown_answer:work_authorization")

    validation = context.validation
    resume_ok = bool(validation.get("valid")) and bool(validation.get("facts", {}).get("valid", True)) and bool(validation.get("ats", {}).get("valid", True))
    checks.append({"gate": "resume_grounding", "result": "pass" if resume_ok else "blocker", "evidence": validation})
    if not resume_ok:
        blockers.append("resume_invalid")

    unknown_fields = [field.key for field in (required_fields or []) if field.required and not (field.value or (field.answer and field.answer.answer))]
    unsupported_fields = [field.key for field in (required_fields or []) if field.field_type not in {"text", "textarea", "email", "tel", "url", "select", "radio", "checkbox", "date"}]
    if unsupported_fields:
        checks.append({"gate": "form", "result": "blocker", "evidence": unsupported_fields})
        blockers.append("unsupported_form")
    elif unknown_fields:
        checks.append({"gate": "required_answers", "result": "unknown", "evidence": unknown_fields})
        blockers.extend(f"unknown_answer:{item}" for item in unknown_fields)
    else:
        checks.append({"gate": "required_answers", "result": "pass", "evidence": []})

    form_analyzed = context.form is not None
    if not form_analyzed:
        checks.append({"gate": "form", "result": "not_analyzed", "evidence": "No ApplicationForm has been inspected yet."})
    if "unsupported_form" in blockers:
        decision = ApplicationState.UNSUPPORTED_FORM
    elif unknown_fields or unknown_fit:
        decision = ApplicationState.NEEDS_ANSWER
    elif not form_analyzed:
        decision = ApplicationState.READY_FOR_REVIEW
    elif context.policy.autonomy.get("fill_forms", "review") != "auto":
        decision = ApplicationState.READY_FOR_REVIEW
    elif fit_blockers or not resume_ok:
        decision = ApplicationState.REJECTED
    else:
        decision = ApplicationState.READY_TO_APPLY
    if fit_blockers or not resume_ok:
        decision = ApplicationState.REJECTED
    requires_review = decision == ApplicationState.READY_FOR_REVIEW or context.policy.autonomy.get("submit", "manual") != "auto"
    checks.append({"gate": "authorization", "result": "review" if requires_review else "authorized", "evidence": {"fill_forms": context.policy.autonomy.get("fill_forms", "review"), "submit": context.policy.autonomy.get("submit", "manual")}})
    return ApplicationReadiness(decision, decision == ApplicationState.READY_TO_APPLY, requires_review, checks, blockers)
