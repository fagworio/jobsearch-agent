"""Submit V2 pelo Chrome real e pela autorização de ação única.

Este módulo não inicia navegador, não usa Playwright e não observa a rede.
Ele prepara fatos no backend, envia comandos tipados à extensão, persiste a
autorização antes do clique e classifica somente a evidência final da página.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from .answers import AnswerLibrary, resolve
from .application import Application, SubmitAuthorization, fingerprint, greenhouse_form_fingerprint
from .artifacts import ResumeArtifact
from .ats.greenhouse import GreenhouseAdapter
from .browser import NativeMessagingClient
from .challenges import ChallengeState
from .fill import _action_for, _snapshot_field_value
from .models import State

POST_SUBMIT_SETTLE_MS = 20_000


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


def _may_retry(existing: dict[str, Any]) -> bool:
    if not existing:
        return True
    return False


def _refusal(existing: dict[str, Any], path: Path) -> SubmitReport:
    return SubmitReport(
        state=SubmitState.REFUSED,
        reason="already_submitted" if existing.get("outcome") == "SUBMITTED" else "previous_attempt_unresolved",
        job_url=str(existing.get("job_url") or ""),
        apply_url=str(existing.get("apply_url") or ""),
        marker=str(path),
        write_possible_at=str(existing.get("write_possible_at") or ""),
        evidence=str(existing.get("evidence") or ""),
        submission_writes=1,
    )


def _wait_for_human(browser: NativeMessagingClient, budget_ms: int) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, budget_ms) / 1000
    latest: dict[str, Any] = {}
    while True:
        latest = browser.challenge_state()
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
    resume: str = "",
    store: str = "data/v2-submissions",
    settle_ms: int = POST_SUBMIT_SETTLE_MS,
    human_wait_ms: int = 0,
    headless: bool = False,
    timeout_ms: float = 45_000,
) -> SubmitReport:
    """Preenche, autoriza e aciona o submit exatamente uma vez no Chrome."""

    del headless, timeout_ms
    path = marker_path(store, url)
    existing = read_marker(path)
    if not _may_retry(existing):
        return _refusal(existing, path)
    if not resume:
        return SubmitReport(state=SubmitState.NO_WRITE, reason="resume_required", job_url=url, marker=str(path))

    try:
        artifact = ResumeArtifact.from_path(resume)
        with NativeMessagingClient() as browser:
            snapshot = browser.inspect_form()
            form = GreenhouseAdapter().to_form(snapshot)
            apply_url = str(snapshot.get("url") or url)
            resolution = resolve(form, approved=approved, profile=profile, rules=rules, library=library)
            if not resolution.complete:
                return SubmitReport(
                    state=SubmitState.NO_WRITE,
                    reason="missing_answer",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    marker=str(path),
                )

            resume_field = next((item for item in form.fields if item.kind == "file"), None)
            if resume_field is None:
                return SubmitReport(
                    state=SubmitState.NO_WRITE,
                    reason="resume_field_not_found",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    marker=str(path),
                )
            uploaded = browser.upload_artifact(artifact.to_payload(resume_field.key))
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
            for field in form.fields:
                if field.key not in resolution.answers:
                    continue
                value = resolution.answers[field.key]
                observed = browser.fill_form({
                    "field_id": field.key,
                    "action": _action_for(field, value),
                    "value": value,
                })
                if _snapshot_field_value(observed, field.key) != value:
                    return SubmitReport(
                        state=SubmitState.NO_WRITE,
                        reason=f"FIELD_MISMATCH: {field.key}",
                        job_url=url,
                        apply_url=apply_url,
                        fields=len(form.fields),
                        verified=verified,
                        resume=resume,
                        resume_attached=True,
                        marker=str(path),
                    )
                verified += 1

            read_back = browser.read_form()
            for field in form.fields:
                if field.key in resolution.answers and _snapshot_field_value(read_back, field.key) != resolution.answers[field.key]:
                    return SubmitReport(
                        state=SubmitState.NO_WRITE,
                        reason=f"FIELD_MISMATCH: {field.key}",
                        job_url=url,
                        apply_url=apply_url,
                        fields=len(form.fields),
                        verified=verified,
                        resume=resume,
                        resume_attached=True,
                        marker=str(path),
                    )

            challenge = browser.challenge_state()
            if challenge.get("state") != "CLEAR" and human_wait_ms:
                challenge = _wait_for_human(browser, human_wait_ms)
            if challenge.get("state") != "CLEAR":
                return SubmitReport(
                    state=SubmitState.HUMAN_REQUIRED,
                    reason=f"human challenge state is {challenge.get('state', 'UNKNOWN')}",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    verified=verified,
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
                "resume_sha256": artifact.sha256,
                "fields": len(form.fields),
                "verified": verified,
                "write_possible_at": write_possible_at,
                "outcome": "",
                "observation_complete": False,
                "click_count": 0,
            }
            _persist(path, record)

            reply = browser.request_submit({"authorization": submitting.authorization.to_payload()})
            record["click_count"] = 1
            deadline = time.monotonic() + max(0, min(settle_ms, 60_000)) / 1000
            result = browser.submit_result()
            while result.get("state") == "SUBMIT_UNKNOWN" and time.monotonic() < deadline:
                time.sleep(0.5)
                result = browser.submit_result()
            final_state = str(result.get("state") or "SUBMIT_UNKNOWN")
            if final_state == "SUBMITTED":
                state, reason, complete = SubmitState.SUBMITTED, "confirmation_observed", True
            elif final_state == "SUBMIT_FAILED":
                state, reason, complete = SubmitState.SUBMIT_FAILED, "failure_observed", True
            else:
                state, reason, complete = SubmitState.SUBMIT_UNKNOWN, "no_decisive_page_evidence", False
            primary = result.get("primary") if isinstance(result.get("primary"), dict) else {}
            evidence = str(primary.get("detail") or "")
            record.update(
                outcome=state.value,
                reason=reason,
                observation_complete=complete,
                final_url=str(primary.get("url") or apply_url),
                evidence=evidence,
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
                resume=resume,
                resume_attached=True,
                marker=str(path),
                write_possible_at=write_possible_at,
                final_url=str(primary.get("url") or apply_url),
                evidence=evidence,
                notes=("submit action dispatched by Chrome Extension exactly once",),
                attempts=1,
                submission_writes=1,
            )
    except Exception as exc:  # noqa: BLE001 - boundary becomes a safe report
        return SubmitReport(state=SubmitState.NO_WRITE, reason=str(exc), job_url=url, marker=str(path))
