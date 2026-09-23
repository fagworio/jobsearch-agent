"""Explicit, provider-scoped submission boundary.

Dry-run execution remains in :mod:`execution` and has no submit action.  This
module is the separate contract for a future live provider adapter: it binds a
single authorization to the exact application materials, persists an attempt
before network I/O, and makes ambiguous outcomes terminal until a human reviews
what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .application import ApplicationDomainError, ApplicationService
from .models import (
    ApplicationEvent,
    ApplicationForm,
    ApplicationState,
    ReviewSnapshot,
    SubmissionAttempt,
    SubmissionIntent,
    ValidationResult,
    now_iso,
)

from .persistence import ApplicationConflict, Database
from .serialization import canonical_json


def compute_answers_fingerprint(form: ApplicationForm) -> str:
    """Hash the resolved answer material that a reviewer sees."""
    payload = []
    for item in sorted(form.fields, key=lambda field: field.key):
        answer = item.answer
        payload.append(
            {
                "key": item.key,
                "value": item.value,
                "attachment_path": item.attachment_path,
                "answer": {
                    "answer": answer.answer,
                    "source": answer.source,
                    "supported_by": sorted(answer.supported_by),
                    "approved": answer.approved,
                    "legal": answer.legal,
                }
                if answer
                else None,
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SubmissionBoundaryError(ValueError):
    """Raised when a live submission would violate its authorization boundary."""


@dataclass(frozen=True)
class LiveNetworkPolicy:
    provider: str
    allowed_origin: str
    allowed_path_pattern: str
    allowed_method: str
    allowed_stage: str
    application_id: str
    submission_intent_id: str

    @classmethod
    def for_submission(cls, provider: str, application_id: str, submission_intent_id: str) -> "LiveNetworkPolicy":
        patterns = {
            "greenhouse": r"^/[^/]+/jobs/[^/]+/?$",
        }
        origins = {
            "greenhouse": "https://boards.greenhouse.io",
        }
        if provider not in origins:
            raise SubmissionBoundaryError(f"no live network policy for provider: {provider}")
        return cls(provider, origins[provider], patterns[provider], "POST", "SUBMIT", application_id, submission_intent_id)

    def validate(self, method: str, url: str, stage: str) -> ValidationResult:
        errors: list[str] = []
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if method.upper() != self.allowed_method.upper():
            errors.append("unexpected submission method")
        if origin != self.allowed_origin:
            errors.append("unexpected submission origin")
        if not re.fullmatch(self.allowed_path_pattern, parsed.path):
            errors.append("unexpected submission path")
        if stage != self.allowed_stage:
            errors.append("unexpected submission stage")
        if parsed.username or parsed.password:
            errors.append("submission URL must not contain credentials")
        return ValidationResult(not errors, "OK" if not errors else "BLOCKED_UNEXPECTED_WRITE", errors)


@dataclass(frozen=True)
class SubmissionVerification:
    status: str
    confirmation_type: str = ""
    evidence: dict[str, object] = field(default_factory=dict)

    @classmethod
    def confirmed(cls, confirmation_type: str, evidence: dict[str, object] | None = None) -> "SubmissionVerification":
        if not confirmation_type:
            raise SubmissionBoundaryError("confirmed submission requires confirmation evidence")
        return cls("confirmed", confirmation_type, evidence or {})

    @classmethod
    def failed(cls, reason: str = "") -> "SubmissionVerification":
        return cls("failed", "", {"reason": reason} if reason else {})

    @classmethod
    def unknown(cls, reason: str = "") -> "SubmissionVerification":
        return cls("unknown", "", {"reason": reason} if reason else {})


def build_review_snapshot(
    *,
    application_id: str,
    job_id: str,
    company: str,
    title: str,
    provider: str,
    destination: str,
    resume_filename: str,
    resume_sha256: str,
    form_fingerprint: str,
    answers_fingerprint: str,
    resolved_fields: list[dict[str, object]] | None = None,
    manual_questions: list[dict[str, object]] | None = None,
) -> ReviewSnapshot:
    """Build the user-facing review data without storing unnecessary PII in logs."""
    return ReviewSnapshot(
        application_id=application_id,
        job_id=job_id,
        company=company,
        title=title,
        provider=provider,
        destination=destination,
        resume_filename=resume_filename,
        resume_sha256=resume_sha256,
        form_fingerprint=form_fingerprint,
        answers_fingerprint=answers_fingerprint,
        resolved_fields=resolved_fields or [],
        manual_questions=manual_questions or [],
    )


def _expires_at(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _intent_id(application_id: str, job_id: str, provider: str, destination: str, form_fingerprint: str, resume_sha256: str, answers_fingerprint: str) -> str:
    payload = {
        "application_id": application_id,
        "job_id": job_id,
        "provider": provider,
        "destination": destination,
        "form_fingerprint": form_fingerprint,
        "resume_sha256": resume_sha256,
        "answers_fingerprint": answers_fingerprint,
    }
    return "intent-" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:24]


def _path_hash(url: str) -> str:
    return hashlib.sha256(urlsplit(url).path.encode("utf-8")).hexdigest()[:16]


_SAFE_EVIDENCE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _safe_evidence_token(value: object) -> str:
    text = str(value)
    return text if _SAFE_EVIDENCE_TOKEN.fullmatch(text) else "redacted"


def _redacted_evidence(verification: SubmissionVerification) -> dict[str, object]:
    evidence: dict[str, object] = {}
    if verification.confirmation_type:
        evidence["confirmation_type"] = _safe_evidence_token(verification.confirmation_type)
    status_code = verification.evidence.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool) and 100 <= status_code <= 599:
        evidence["status_code"] = status_code
    if "provider_status" in verification.evidence:
        evidence["provider_status"] = _safe_evidence_token(verification.evidence["provider_status"])
    return evidence


class SubmissionService:
    def __init__(self, database: Database):
        self.database = database

    def create_intent(
        self,
        *,
        application_id: str,
        job_id: str,
        provider: str,
        destination: str,
        form_fingerprint: str,
        resume_sha256: str,
        answers_fingerprint: str,
        expires_in_seconds: int,
        require_ready: bool = True,
        allow_insecure_destination: bool = False,
    ) -> SubmissionIntent:
        application = self.database.get_application(application_id)
        if not application or application.job_id != job_id:
            raise SubmissionBoundaryError("application and job do not match")
        if require_ready and application.state not in {ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REACHED}:
            raise SubmissionBoundaryError(f"submission intent requires READY_TO_APPLY: {application.state.value}")
        parsed = urlsplit(destination)
        insecure_loopback = allow_insecure_destination and parsed.hostname in {"127.0.0.1", "localhost"}
        if (parsed.scheme != "https" and not insecure_loopback) or not parsed.netloc or parsed.username or parsed.password:
            raise SubmissionBoundaryError("submission destination must be HTTPS, except controlled loopback tests")
        if expires_in_seconds <= 0:
            raise SubmissionBoundaryError("submission intent expiration must be positive")
        if not all((form_fingerprint, resume_sha256, answers_fingerprint)):
            raise SubmissionBoundaryError("submission intent requires form, resume and answers fingerprints")
        intent = SubmissionIntent(
            id=_intent_id(application_id, job_id, provider, destination, form_fingerprint, resume_sha256, answers_fingerprint),
            application_id=application_id,
            job_id=job_id,
            provider=provider,
            destination=destination,
            form_fingerprint=form_fingerprint,
            resume_sha256=resume_sha256,
            answers_fingerprint=answers_fingerprint,
            expires_at=_expires_at(expires_in_seconds),
        )
        existing = self.database.get_submission_intent(intent.id)
        if existing:
            return existing
        self.database.save_submission_intent(intent)
        return intent

    def save_review_snapshot(self, snapshot: ReviewSnapshot) -> ReviewSnapshot:
        self.database.save_review_snapshot(snapshot)
        return snapshot

    def authorize_submission(self, intent_id: str) -> SubmissionIntent:
        intent = self.database.get_submission_intent(intent_id)
        if not intent:
            raise SubmissionBoundaryError(f"submission intent not found: {intent_id}")
        if intent.status != "CREATED":
            raise SubmissionBoundaryError(f"submission intent cannot be authorized from {intent.status}")
        if _expired(intent.expires_at):
            raise SubmissionBoundaryError("submission intent has expired")
        application = self.database.get_application(intent.application_id)
        if not application:
            raise SubmissionBoundaryError("application not found for submission intent")
        if application.state not in {ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REACHED}:
            raise ApplicationDomainError(f"explicit authorization requires READY_TO_APPLY, got {application.state.value}")
        snapshot = self.database.get_review_snapshot(intent.application_id)
        if not snapshot:
            raise SubmissionBoundaryError("explicit authorization requires persisted review snapshot")
        if (
            snapshot.job_id != intent.job_id
            or snapshot.provider != intent.provider
            or snapshot.destination != intent.destination
            or snapshot.resume_sha256 != intent.resume_sha256
            or snapshot.form_fingerprint != intent.form_fingerprint
            or snapshot.answers_fingerprint != intent.answers_fingerprint
        ):
            raise SubmissionBoundaryError("review snapshot does not match submission intent")
        intent.status = "AUTHORIZED"
        intent.authorized_at = now_iso()
        ApplicationService(self.database).transition(
            intent.application_id,
            ApplicationState.SUBMIT_AUTHORIZED,
            "submission_authorized",
            {"intent_id": intent.id},
        )
        self.database.save_submission_intent(intent)
        return intent

    def begin_submission(
        self,
        intent_id: str,
        *,
        current_form_fingerprint: str,
        current_resume_sha256: str,
        current_answers_fingerprint: str,
        policy: LiveNetworkPolicy,
        method: str,
        url: str,
    ) -> SubmissionAttempt:
        intent = self.database.get_submission_intent(intent_id)
        if not intent:
            raise SubmissionBoundaryError(f"submission intent not found: {intent_id}")
        if intent.status != "AUTHORIZED":
            if intent.status == "SUBMITTING":
                raise SubmissionBoundaryError("submission is already submitting")
            if intent.status in {"UNKNOWN", "SUBMITTED"}:
                raise SubmissionBoundaryError(f"duplicate submission blocked: {intent.status.casefold()}")
            raise SubmissionBoundaryError("submission requires explicit authorization")
        if _expired(intent.expires_at):
            raise SubmissionBoundaryError("submission authorization has expired")
        if policy.application_id != intent.application_id or policy.submission_intent_id != intent.id or policy.provider != intent.provider:
            raise SubmissionBoundaryError("live network policy is not bound to submission intent")
        network_result = policy.validate(method, url, "SUBMIT")
        if not network_result.valid:
            raise SubmissionBoundaryError("; ".join(network_result.errors))
        if url != intent.destination:
            raise SubmissionBoundaryError("submission destination does not match intent")
        if current_form_fingerprint != intent.form_fingerprint:
            raise SubmissionBoundaryError("form fingerprint does not match submission intent")
        if current_resume_sha256 != intent.resume_sha256:
            raise SubmissionBoundaryError("resume SHA256 does not match submission intent")
        if current_answers_fingerprint != intent.answers_fingerprint:
            raise SubmissionBoundaryError("answers fingerprint does not match submission intent")
        application = self.database.get_application(intent.application_id)
        if not application or application.state != ApplicationState.SUBMIT_AUTHORIZED:
            raise SubmissionBoundaryError("application is not authorized for submission")
        for previous in self.database.list_submission_attempts(intent.application_id):
            if previous.status in {"SUBMITTING", "SUBMITTED", "SUBMIT_UNKNOWN"}:
                raise SubmissionBoundaryError(f"duplicate submission blocked: {previous.status.casefold()}")
        attempt = SubmissionAttempt(
            id="attempt-" + uuid4().hex,
            intent_id=intent.id,
            application_id=intent.application_id,
            provider=intent.provider,
            method=method.upper(),
            origin=f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
            path_hash=_path_hash(url),
        )
        intent.status = "SUBMITTING"
        event = ApplicationEvent(
            intent.application_id,
            ApplicationState.SUBMIT_AUTHORIZED,
            ApplicationState.SUBMITTING,
            "submission_started",
            {"intent_id": intent.id, "attempt_id": attempt.id},
        )
        try:
            self.database.begin_submission_attempt(intent, attempt, event)
        except ApplicationConflict as exc:
            raise SubmissionBoundaryError(str(exc)) from exc
        return attempt

    def record_result(self, attempt_id: str, verification: SubmissionVerification) -> SubmissionAttempt:
        attempt = self.database.get_submission_attempt(attempt_id)
        if not attempt:
            raise SubmissionBoundaryError(f"submission attempt not found: {attempt_id}")
        if attempt.status != "SUBMITTING":
            raise SubmissionBoundaryError(f"submission attempt is already complete: {attempt.status}")
        intent = self.database.get_submission_intent(attempt.intent_id)
        if not intent:
            raise SubmissionBoundaryError(f"submission intent not found: {attempt.intent_id}")
        targets = {"confirmed": (ApplicationState.SUBMITTED, "SUBMITTED"), "failed": (ApplicationState.SUBMIT_FAILED, "FAILED"), "unknown": (ApplicationState.SUBMIT_UNKNOWN, "UNKNOWN")}
        if verification.status not in targets:
            raise SubmissionBoundaryError(f"unsupported submission verification: {verification.status}")
        target, intent_status = targets[verification.status]
        attempt.status = target.value
        attempt.completed_at = now_iso()
        attempt.evidence = _redacted_evidence(verification)
        intent.status = intent_status
        event = ApplicationEvent(
            attempt.application_id,
            ApplicationState.SUBMITTING,
            target,
            f"submission_{verification.status}",
            {"attempt_id": attempt.id, "evidence": attempt.evidence},
        )
        try:
            self.database.complete_submission_attempt(attempt, intent, target, event)
        except ApplicationConflict as exc:
            raise SubmissionBoundaryError(str(exc)) from exc
        return attempt
