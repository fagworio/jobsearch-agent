"""Submit V2 pelo Chrome real e pela autorização de ação única.

Este módulo não inicia navegador, não usa Playwright e não observa a rede.
Ele prepara fatos no backend, envia comandos tipados à extensão, persiste a
autorização antes do clique e classifica somente a evidência final da página.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from .answers import AnswerLibrary, resolve
from .application import Application, SubmitAuthorization, fingerprint, greenhouse_form_fingerprint
from .artifacts import ResumeArtifact
from .ats.greenhouse import GreenhouseAdapter
from .browser import NativeMessagingClient
from .challenges import ChallengeState
from .confirmation import ConfirmationState, classify_browser_result
from .facts import FactStore
from .fill import _action_for, _snapshot_field_value
from .models import Form, MissingQuestion, State
from .version import ENGINE_VERSION

POST_SUBMIT_SETTLE_MS = 20_000


def _values_match(actual: str, expected: str) -> bool:
    """Compare provider read-back labels without treating whitespace as data."""

    def compact(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "")).casefold()

    return compact(actual) == compact(expected)


class SubmitState(str, Enum):
    SUBMITTED = "SUBMITTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    NO_WRITE = "NO_WRITE"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class SubmitReport:
    state: SubmitState
    reason: str = ""
    job_url: str = ""
    apply_url: str = ""
    fields: int = 0
    verified: int = 0
    resolved_fact_ids: dict[str, str] = field(default_factory=dict)
    missing_facts: tuple[str, ...] = ()
    missing_questions: tuple[MissingQuestion, ...] = ()
    resume: str = ""
    resume_attached: bool = False
    marker: str = ""
    write_possible_at: str = ""
    post_requests: tuple[str, ...] = ()
    other_post_requests: tuple[str, ...] = ()
    final_url: str = ""
    evidence: str = ""
    captcha: bool = False
    response_status: int = 0
    response_snippet: str = ""
    notes: tuple[str, ...] = ()
    attempts: int = 0
    submission_writes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "job_url": self.job_url,
            "apply_url": self.apply_url,
            "fields": self.fields,
            "verified": self.verified,
            "resolved_fact_ids": dict(self.resolved_fact_ids or {}),
            "missing_facts": list(self.missing_facts),
            "missing_questions": [item.to_dict() for item in self.missing_questions],
            "resume": self.resume,
            "resume_attached": self.resume_attached,
            "marker": self.marker,
            "write_possible_at": self.write_possible_at,
            "post_requests": list(self.post_requests),
            "other_post_requests": list(self.other_post_requests),
            "final_url": self.final_url,
            "evidence": self.evidence,
            "captcha": self.captcha,
            "response_status": self.response_status,
            "response_snippet": self.response_snippet,
            "notes": list(self.notes),
            "attempts": self.attempts,
            "submission_writes": self.submission_writes,
        }


def marker_path(store: str, job_url: str) -> Path:
    key = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:16]
    return Path(store) / f"{key}.json"


def read_marker(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - marcador ilegível bloqueia nova ação
        return {"outcome": "UNREADABLE"}
    return payload if isinstance(payload, dict) else {"outcome": "UNREADABLE"}


def _persist(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        import os

        os.fsync(handle.fileno())


def _may_retry(existing: dict[str, Any], *, retry_failed: bool = False) -> bool:
    if not existing:
        return True
    # An explicit operator retry is safe only after the page itself exposed a
    # validation failure. Confirmed and indeterminate outcomes remain a hard
    # stop because retrying them could create a duplicate application.
    return retry_failed and existing.get("outcome") == SubmitState.SUBMIT_FAILED.value


def _refusal(existing: dict[str, Any], path: Path) -> SubmitReport:
    return SubmitReport(
        state=SubmitState.REFUSED,
        reason="already_submitted" if existing.get("outcome") == "SUBMITTED" else "previous_attempt_unresolved",
        job_url=str(existing.get("job_url") or ""),
        apply_url=str(existing.get("apply_url") or ""),
        marker=str(path),
        write_possible_at=str(existing.get("write_possible_at") or ""),
        evidence=str(existing.get("evidence") or ""),
        # A refusal is reconciliation only: no new submit authorization or
        # browser click happened in this invocation.
        submission_writes=0,
    )


def _hydrate_choice_options(
    browser: NativeMessagingClient,
    snapshot: dict[str, Any],
    *,
    tab_id: int | None,
) -> dict[str, Any]:
    """Abre choices fechados somente para observá-los antes da resolução."""

    raw_fields = snapshot.get("fields")
    if not isinstance(raw_fields, list):
        return snapshot
    hydrated = dict(snapshot)
    fields: list[dict[str, Any]] = []
    for raw in raw_fields:
        if not isinstance(raw, dict):
            fields.append(raw)
            continue
        field = dict(raw)
        field_type = str(field.get("type") or "")
        prompt = str(field.get("label") or field.get("prompt") or "").casefold()
        deterministic_combobox = (
            "region where you currently live" in prompt
            or "current region" in prompt
        )
        # React Select menus are lazy, virtualized and rendered in a portal.
        # Opening them during a full-form pass can leave a stale provider
        # dropdown active and invalidate subsequent field identities. Known
        # answers can still be selected by the typed fill path. Only
        # deterministic comboboxes are opened; unknown comboboxes remain
        # NEEDS_INPUT until a real option is observed.
        if (
            field_type in {"select", "radio", "checkbox_group"}
            or (field_type == "combobox" and deterministic_combobox)
        ) and not field.get("options"):
            try:
                result = browser.inspect_field_options(str(field.get("id") or ""), tab_id=tab_id)
                options = result.get("options")
                if isinstance(options, list) and all(isinstance(option, str) for option in options):
                    field["options"] = options
            except (RuntimeError, ValueError):
                # An unopened/unsupported widget remains a genuine unresolved
                # field; never invent an option to make it pass.
                pass
        fields.append(field)
    hydrated["fields"] = fields
    return hydrated


def _wait_for_human(browser: NativeMessagingClient, budget_ms: int, *, tab_id: int | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, budget_ms) / 1000
    latest: dict[str, Any] = {}
    while True:
        latest = browser.challenge_state(tab_id=tab_id) if tab_id is not None else browser.challenge_state()
        if latest.get("state") == "CLEAR" or time.monotonic() >= deadline:
            return latest
        time.sleep(0.5)


def submit(
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    library: AnswerLibrary | None = None,
    facts: FactStore | None = None,
    resume: str = "",
    store: str = "data/v2-submissions",
    settle_ms: int = POST_SUBMIT_SETTLE_MS,
    human_wait_ms: int = 0,
    headless: bool = False,
    timeout_ms: float = 45_000,
    tab_id: int | None = None,
    provider: str = "greenhouse",
    canonical_job_id: str = "",
    resume_identity: str = "",
    retry_failed: bool = False,
) -> SubmitReport:
    """Preenche, autoriza e aciona o submit exatamente uma vez no Chrome."""

    del headless, timeout_ms
    path = marker_path(store, url)
    # This boundary is persisted immediately before the one-shot browser
    # action. Any transport/runtime failure after it may have happened after
    # the provider accepted the click, so it must never be reported as
    # ``NO_WRITE``.
    write_possible_at = ""
    existing = read_marker(path)
    if not _may_retry(existing, retry_failed=retry_failed):
        return _refusal(existing, path)
    if not resume:
        return SubmitReport(state=SubmitState.NO_WRITE, reason="resume_required", job_url=url, marker=str(path))

    try:
        artifact = ResumeArtifact.from_path(resume)
        with NativeMessagingClient() as browser:
            auth = browser.auth_state(tab_id=tab_id) if tab_id is not None else browser.auth_state()
            if auth.get("state") in {"LOGIN_REQUIRED", "LOGIN_PENDING"}:
                return SubmitReport(
                    state=SubmitState.NO_WRITE,
                    reason="login_required" if auth.get("state") == "LOGIN_REQUIRED" else "login_pending",
                    job_url=url,
                    resume=resume,
                    marker=str(path),
                )
            snapshot = browser.inspect_form(tab_id=tab_id) if tab_id is not None else browser.inspect_form()
            snapshot = _hydrate_choice_options(browser, snapshot, tab_id=tab_id)
            form = GreenhouseAdapter().to_form(snapshot)
            apply_url = str(snapshot.get("url") or url)
            # A resume is an artifact, not an answer. Easy Apply can expose
            # the already-attached resume as a synthetic required file field;
            # resolve only actual questions so the submit path never tries to
            # write a filename back into a file control.
            answer_form = Form(tuple(field for field in form.fields if field.kind != "file"))
            resolution = resolve(answer_form, approved=approved, profile=profile, rules=rules, library=library, facts=facts)
            if not resolution.complete:
                return SubmitReport(
                    state=SubmitState.NO_WRITE,
                    reason="missing_answer",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    resolved_fact_ids=resolution.resolved_fact_ids,
                    missing_facts=tuple(resolution.missing_fact_ids.values()),
                    missing_questions=resolution.missing_questions,
                    marker=str(path),
                )

            resume_field = next(
                (item for item in form.fields if item.kind == "file" and item.required),
                None,
            ) or next((item for item in form.fields if item.kind == "file"), None)
            if resume_field is None:
                return SubmitReport(
                    state=SubmitState.NO_WRITE,
                    reason="resume_field_not_found",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    marker=str(path),
                )
            # Greenhouse Easy Apply removes the file input after an upload and
            # leaves a visible filename plus a "Remove file" control. In that
            # state the attachment is already present and must not be replaced
            # merely because the DOM no longer exposes an input.
            existing_resume = _snapshot_field_value(snapshot, resume_field.key).strip()
            if existing_resume:
                uploaded = snapshot
            else:
                upload_payload = artifact.to_payload(resume_field.key)
                uploaded = browser.upload_artifact(upload_payload, tab_id=tab_id) if tab_id is not None else browser.upload_artifact(upload_payload)
                if not _snapshot_field_value(uploaded, resume_field.key):
                    return SubmitReport(
                        state=SubmitState.NO_WRITE,
                        reason="resume_upload_readback_failed",
                        job_url=url,
                        apply_url=apply_url,
                        fields=len(form.fields),
                        resume=resume,
                        marker=str(path),
                    )

            verified = 0
            for field in answer_form.fields:
                if field.key not in resolution.answers:
                    continue
                value = resolution.answers[field.key]
                # Preserve values already confirmed in the live form. This is
                # important for provider widgets such as React Select: the
                # browser-native interaction may be complete even when the
                # content-script mutation path cannot reproduce it.
                if _values_match(_snapshot_field_value(uploaded, field.key), value):
                    verified += 1
                    continue
                fill_payload = {
                    "field_id": field.key,
                    "action": _action_for(field, value),
                    "value": value,
                }
                observed = browser.fill_form(fill_payload, tab_id=tab_id) if tab_id is not None else browser.fill_form(fill_payload)
                if not _values_match(_snapshot_field_value(observed, field.key), value):
                    return SubmitReport(
                        state=SubmitState.NO_WRITE,
                        reason=f"FIELD_MISMATCH: {field.key}",
                        job_url=url,
                        apply_url=apply_url,
                        fields=len(form.fields),
                        verified=verified,
                        resolved_fact_ids=resolution.resolved_fact_ids,
                        resume=resume,
                        resume_attached=True,
                        marker=str(path),
                    )
                verified += 1

            read_back = browser.read_form(tab_id=tab_id) if tab_id is not None else browser.read_form()
            for field in answer_form.fields:
                if field.key in resolution.answers and not _values_match(
                    _snapshot_field_value(read_back, field.key),
                    resolution.answers[field.key],
                ):
                    return SubmitReport(
                        state=SubmitState.NO_WRITE,
                        reason=f"FIELD_MISMATCH: {field.key}",
                        job_url=url,
                        apply_url=apply_url,
                        fields=len(form.fields),
                        verified=verified,
                        resolved_fact_ids=resolution.resolved_fact_ids,
                        resume=resume,
                        resume_attached=True,
                        marker=str(path),
                    )

            challenge = browser.challenge_state(tab_id=tab_id) if tab_id is not None else browser.challenge_state()
            if challenge.get("state") != "CLEAR" and human_wait_ms:
                challenge = _wait_for_human(browser, human_wait_ms, tab_id=tab_id)
            if challenge.get("state") != "CLEAR":
                return SubmitReport(
                    state=SubmitState.HUMAN_REQUIRED,
                    reason=f"human challenge state is {challenge.get('state', 'UNKNOWN')}",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    verified=verified,
                    resolved_fact_ids=resolution.resolved_fact_ids,
                    resume=resume,
                    resume_attached=True,
                    marker=str(path),
                    captcha=True,
                )

            form_fp = greenhouse_form_fingerprint(read_back)
            answers_fp = fingerprint(resolution.answers)
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            authorization = SubmitAuthorization.issue(
                hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                form_fp,
                answers_fp,
                artifact.sha256,
                expires_at=expires_at,
                tab_id=tab_id,
                provider=provider if tab_id is not None else "",
                canonical_job_id=canonical_job_id if tab_id is not None else "",
            )
            application = Application(
                id=authorization.application_id,
                state=State.READY_TO_SUBMIT,
                form_fingerprint=form_fp,
                answers_fingerprint=answers_fp,
                resume_sha256=artifact.sha256,
                authorization=authorization,
            ).observe_challenge(ChallengeState.CLEAR)
            submitting = application.begin_submit()
            write_possible_at = datetime.now(timezone.utc).isoformat()
            record: dict[str, Any] = {
                "job_url": url,
                "apply_url": apply_url,
                "resume_path": resume,
                "resume_identity": resume_identity,
                "resume_sha256": artifact.sha256,
                "fields": len(form.fields),
                "verified": verified,
                "answers_fingerprint": answers_fp,
                "job_id": canonical_job_id,
                "engine_version": ENGINE_VERSION,
                "form_fingerprint": form_fp,
                "write_possible_at": write_possible_at,
                "outcome": "",
                "observation_complete": False,
                "click_count": 0,
            }
            _persist(path, record)

            request_payload = {"authorization": submitting.authorization.to_payload()}
            reply = browser.request_submit(request_payload, tab_id=tab_id) if tab_id is not None else browser.request_submit(request_payload)
            record["click_count"] = 1
            deadline = time.monotonic() + max(0, min(settle_ms, 60_000)) / 1000
            result = browser.submit_result(tab_id=tab_id) if tab_id is not None else browser.submit_result()
            while result.get("state") == "SUBMIT_UNKNOWN" and time.monotonic() < deadline:
                time.sleep(0.5)
                result = browser.submit_result(tab_id=tab_id) if tab_id is not None else browser.submit_result()
            confirmation = classify_browser_result(result)
            if confirmation.state is ConfirmationState.SUBMITTED:
                state, reason, complete = SubmitState.SUBMITTED, "confirmation_observed", True
            elif confirmation.state is ConfirmationState.SUBMIT_FAILED:
                state, reason, complete = SubmitState.SUBMIT_FAILED, "failure_observed", True
            else:
                state, reason, complete = SubmitState.SUBMIT_UNKNOWN, "no_decisive_page_evidence", False
            primary = confirmation.primary
            evidence = primary.detail
            record.update(
                outcome=state.value,
                reason=reason,
                observation_complete=complete,
                final_url=primary.url or apply_url,
                evidence=evidence,
                confirmation=confirmation.to_dict(),
                response=result,
                extension_reply=reply,
            )
            _persist(path, record)
            return SubmitReport(
                state=state,
                reason=reason,
                job_url=url,
                apply_url=apply_url,
                fields=len(form.fields),
                verified=verified,
                resolved_fact_ids=resolution.resolved_fact_ids,
                resume=resume,
                resume_attached=True,
                marker=str(path),
                write_possible_at=write_possible_at,
                final_url=primary.url or apply_url,
                evidence=evidence,
                notes=("submit action dispatched by Chrome Extension exactly once",),
                attempts=1,
                submission_writes=1,
            )
    except Exception as exc:  # noqa: BLE001 - boundary becomes a safe report
        state = SubmitState.SUBMIT_UNKNOWN if write_possible_at else SubmitState.NO_WRITE
        return SubmitReport(
            state=state,
            reason=str(exc),
            job_url=url,
            marker=str(path),
            write_possible_at=write_possible_at,
            attempts=1 if write_possible_at else 0,
            submission_writes=1 if write_possible_at else 0,
            notes=("submit boundary was crossed; reconciliation is required",) if write_possible_at else (),
        )
