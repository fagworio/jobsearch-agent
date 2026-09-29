"""Estado e autorização de submit da V2, sem conhecimento de ATS."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping
from uuid import uuid4

from .challenges import ChallengeState
from .models import State


class ApplicationStateError(ValueError):
    """Transição inválida ou autorização incompatível."""


def fingerprint(value: Mapping[str, Any] | list[Any] | tuple[Any, ...]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def greenhouse_form_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Calcula o fingerprint estrutural compartilhado com a extensão."""

    raw_fields = snapshot.get("fields", ())
    if not isinstance(raw_fields, (list, tuple)):
        raise ApplicationStateError("Greenhouse snapshot fields must be a list")
    fields: list[dict[str, Any]] = []
    for raw in raw_fields:
        if not isinstance(raw, Mapping):
            raise ApplicationStateError("Greenhouse snapshot field must be an object")
        fields.append({
            "id": raw.get("id", ""),
            "type": raw.get("type", ""),
            "label": raw.get("label", ""),
            "required": raw.get("required", False),
            "options": list(raw.get("options", ())),
        })
    return fingerprint({"provider": "greenhouse", "page_type": "application", "fields": fields})


_TRANSITIONS: dict[State, frozenset[State]] = {
    State.NEW: frozenset({State.NEEDS_INPUT, State.READY}),
    State.NEEDS_INPUT: frozenset({State.READY}),
    State.READY: frozenset({State.FILLING, State.NEEDS_INPUT}),
    State.FILLING: frozenset({State.FILLED, State.NEEDS_INPUT, State.WAITING_HUMAN}),
    State.FILLED: frozenset({State.WAITING_HUMAN, State.READY_TO_SUBMIT, State.NEEDS_INPUT}),
    State.WAITING_HUMAN: frozenset({State.READY_TO_SUBMIT, State.NEEDS_INPUT}),
    State.READY_TO_SUBMIT: frozenset({State.SUBMITTING, State.WAITING_HUMAN}),
    State.SUBMITTING: frozenset({State.SUBMITTED, State.SUBMIT_UNKNOWN, State.FAILED}),
    State.SUBMIT_UNKNOWN: frozenset(),
    State.FAILED: frozenset(),
    State.SUBMITTED: frozenset(),
}


@dataclass(frozen=True)
class SubmitAuthorization:
    application_id: str
    form_fingerprint: str
    answers_fingerprint: str
    resume_sha256: str
    expires_at: str
    token: str
    used: bool = False
    tab_id: int | None = None
    provider: str = ""
    canonical_job_id: str = ""

    @classmethod
    def issue(
        cls,
        application_id: str,
        form_fingerprint: str,
        answers_fingerprint: str,
        resume_sha256: str,
        *,
        expires_at: str,
        tab_id: int | None = None,
        provider: str = "",
        canonical_job_id: str = "",
    ) -> "SubmitAuthorization":
        for name, value in {
            "application_id": application_id,
            "form_fingerprint": form_fingerprint,
            "answers_fingerprint": answers_fingerprint,
            "resume_sha256": resume_sha256,
            "expires_at": expires_at,
        }.items():
            if not value:
                raise ApplicationStateError(f"{name} is required")
        return cls(
            application_id,
            form_fingerprint,
            answers_fingerprint,
            resume_sha256,
            expires_at,
            uuid4().hex,
            False,
            tab_id,
            provider,
            canonical_job_id,
        )

    def consume(
        self,
        *,
        application_id: str,
        form_fingerprint: str,
        answers_fingerprint: str,
        resume_sha256: str,
        now: datetime | None = None,
    ) -> "SubmitAuthorization":
        if self.used:
            raise ApplicationStateError("submit authorization was already used")
        current = now or datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(self.expires_at)
        if expiry.tzinfo is None or current >= expiry:
            raise ApplicationStateError("submit authorization expired")
        values = (application_id, form_fingerprint, answers_fingerprint, resume_sha256)
        expected = (self.application_id, self.form_fingerprint, self.answers_fingerprint, self.resume_sha256)
        if values != expected:
            raise ApplicationStateError("submit authorization fingerprints do not match")
        return replace(self, used=True)

    def to_payload(self) -> dict[str, str | bool | int | None]:
        payload: dict[str, str | bool | int | None] = {
            "application_id": self.application_id,
            "form_fingerprint": self.form_fingerprint,
            "answers_fingerprint": self.answers_fingerprint,
            "resume_sha256": self.resume_sha256,
            "expires_at": self.expires_at,
            "token": self.token,
            "used": self.used,
        }
        if self.tab_id is not None:
            payload["tab_id"] = self.tab_id
        if self.provider:
            payload["provider"] = self.provider
        if self.canonical_job_id:
            payload["canonical_job_id"] = self.canonical_job_id
        return payload


@dataclass(frozen=True)
class Application:
    id: str
    state: State = State.NEW
    form_fingerprint: str = ""
    answers_fingerprint: str = ""
    resume_sha256: str = ""
    challenge_state: ChallengeState = ChallengeState.UNKNOWN
    authorization: SubmitAuthorization | None = None

    def transition(self, target: State) -> "Application":
        if target not in _TRANSITIONS[self.state]:
            raise ApplicationStateError(f"invalid transition: {self.state.value} -> {target.value}")
        return replace(self, state=target)

    def with_review(
        self,
        *,
        form: Mapping[str, Any] | list[Any] | tuple[Any, ...],
        answers: Mapping[str, Any] | list[Any] | tuple[Any, ...],
        resume_sha256: str,
    ) -> "Application":
        if not resume_sha256:
            raise ApplicationStateError("resume_sha256 is required")
        return replace(
            self,
            form_fingerprint=fingerprint(form),
            answers_fingerprint=fingerprint(answers),
            resume_sha256=resume_sha256,
        )

    def authorize(self, *, expires_at: str) -> "Application":
        if self.state is not State.READY_TO_SUBMIT:
            raise ApplicationStateError("application is not ready to submit")
        authorization = SubmitAuthorization.issue(
            self.id,
            self.form_fingerprint,
            self.answers_fingerprint,
            self.resume_sha256,
            expires_at=expires_at,
        )
        return replace(self, authorization=authorization)

    def observe_challenge(self, state: ChallengeState) -> "Application":
        """Registra somente o estado observado; não executa nenhuma ação."""

        return replace(self, challenge_state=state)

    def begin_submit(self, *, now: datetime | None = None) -> "Application":
        """Consome a autorização e abre a única janela de submit."""

        if self.state is not State.READY_TO_SUBMIT:
            raise ApplicationStateError("application is not ready to submit")
        if self.challenge_state is not ChallengeState.CLEAR:
            raise ApplicationStateError("challenge is not CLEAR")
        if self.authorization is None:
            raise ApplicationStateError("submit authorization is missing")
        consumed = self.authorization.consume(
            application_id=self.id,
            form_fingerprint=self.form_fingerprint,
            answers_fingerprint=self.answers_fingerprint,
            resume_sha256=self.resume_sha256,
            now=now,
        )
        return replace(self, state=State.SUBMITTING, authorization=consumed)
