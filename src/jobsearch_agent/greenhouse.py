"""Controlled Greenhouse submission executor.

This provider adapter owns HTTP behavior. It cannot bypass the domain
submission boundary: the service must authorize the intent, validate the live
network policy, and persist the attempt before this class sends a request.
"""

from __future__ import annotations

from dataclasses import dataclass
import html as html_module
import re
from typing import Mapping, Sequence

import httpx

from .persistence import Database
from .submission import LiveNetworkPolicy, SubmissionBoundaryError, SubmissionService, SubmissionVerification


_CSRF_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("csrf-token", re.compile(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', re.I)),
    ("csrf-token", re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', re.I)),
    ("authenticity_token", re.compile(r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)', re.I)),
    ("authenticity_token", re.compile(r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']authenticity_token["\']', re.I)),
    ("authenticity_token", re.compile(r'"csrfToken"\s*:\s*"([^"]+)"')),
    ("authenticity_token", re.compile(r'"authenticity_token"\s*:\s*"([^"]+)"')),
)


def csrf_field(markup: str) -> tuple[str, str]:
    """Extrai o campo anti-CSRF do HTML, se o board publicar um.

    O HTML pode vir escapado (``&lt;meta ...&gt;``), entao desescapa antes de
    casar; a comparacao e feita sobre texto normal.
    """
    text = html_module.unescape(markup or "")
    for name, pattern in _CSRF_PATTERNS:
        match = pattern.search(text)
        if match:
            return name, match.group(1)
    return "", ""


_CONFIRMATION_MARKERS = (
    "application submitted",
    "application has been submitted",
    "your application was submitted",
    "thank you for applying",
    "thanks for applying",
    "we received your application",
    "we have received your application",
)


@dataclass(frozen=True)
class SubmissionExecutionResult:
    status: str
    attempt_id: str
    http_status: int | None = None
    error: str = ""
    files_sent: int = 0
    fields_sent: int = 0


class GreenhouseSubmissionExecutor:
    provider = "greenhouse"

    def __init__(self, database: Database, *, timeout: float = 10.0, client: httpx.Client | None = None):
        if timeout <= 0:
            raise ValueError("submission timeout must be positive")
        if client is not None and bool(getattr(client, "follow_redirects", False)):
            raise ValueError("Greenhouse submission client must not follow redirects")
        self.database = database
        self.timeout = timeout
        self.client = client

    def submit(
        self,
        intent_id: str,
        *,
        current_form_fingerprint: str,
        current_resume_sha256: str,
        current_answers_fingerprint: str,
        policy: LiveNetworkPolicy,
        fields: Mapping[str, str | Sequence[str]],
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        session_url: str = "",
    ) -> SubmissionExecutionResult:
        intent = self.database.get_submission_intent(intent_id)
        if not intent:
            raise SubmissionBoundaryError(f"submission intent not found: {intent_id}")
        if intent.provider != self.provider:
            raise SubmissionBoundaryError(f"Greenhouse executor cannot handle provider: {intent.provider}")
        if not fields and not files:
            raise SubmissionBoundaryError("submission requires at least one field or file part")
        service = SubmissionService(self.database)
        attempt = service.begin_submission(
            intent_id,
            current_form_fingerprint=current_form_fingerprint,
            current_resume_sha256=current_resume_sha256,
            current_answers_fingerprint=current_answers_fingerprint,
            policy=policy,
            method="POST",
            url=intent.destination,
        )
        response: httpx.Response | None = None
        owned_client = self.client is None
        client = self.client or httpx.Client(follow_redirects=False, timeout=self.timeout)
        payload = dict(fields)
        try:
            # Handshake: o form real carrega um token anti-CSRF e cookies de
            # sessao. Sem eles o endpoint devolve 400 Bad Request. O cliente
            # guarda os cookies entre as duas chamadas.
            if session_url:
                try:
                    handshake = client.get(session_url, timeout=self.timeout)
                    token_name, token_value = csrf_field(handshake.text)
                    if token_name and token_value:
                        payload[token_name] = token_value
                except httpx.HTTPError:
                    pass
            # Multipart only when a file part exists: Greenhouse accepts the
            # namespaced form fields and the resume document in one request.
            response = client.post(
                intent.destination,
                data=payload,
                files=dict(files) if files else None,
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            completed = service.record_result(attempt.id, SubmissionVerification.unknown("request timeout"))
            return SubmissionExecutionResult(completed.status, completed.id, error="request timeout")
        except httpx.TransportError:
            completed = service.record_result(attempt.id, SubmissionVerification.unknown("transport uncertainty"))
            return SubmissionExecutionResult(completed.status, completed.id, error="transport uncertainty")
        finally:
            if owned_client:
                client.close()

        files_sent = len(files or {})
        fields_sent = len(fields)
        status_code = response.status_code
        if 200 <= status_code < 300:
            provider_status = self._provider_status(response)
            if provider_status in {"submitted", "accepted", "success"}:
                completed = service.record_result(
                    attempt.id,
                    SubmissionVerification.confirmed(
                        "provider_response",
                        {"status_code": status_code, "provider_status": provider_status},
                    ),
                )
                return SubmissionExecutionResult(completed.status, completed.id, status_code, files_sent=files_sent, fields_sent=fields_sent)
            completed = service.record_result(attempt.id, SubmissionVerification.unknown("confirmation missing"))
            return SubmissionExecutionResult(completed.status, completed.id, status_code, "confirmation missing", files_sent, fields_sent)

        if 300 <= status_code < 400:
            completed = service.record_result(
                attempt.id,
                SubmissionVerification.unknown("redirect without confirmation"),
            )
            return SubmissionExecutionResult(completed.status, completed.id, status_code, "redirect without confirmation", files_sent, fields_sent)

        completed = service.record_result(
            attempt.id,
            SubmissionVerification.failed("provider rejected submission"),
        )
        return SubmissionExecutionResult(completed.status, completed.id, status_code, "provider rejected submission", files_sent, fields_sent)

    @staticmethod
    def _provider_status(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return _html_confirmation_status(response)
        if not isinstance(body, dict):
            return ""
        return str(body.get("status", "")).casefold().strip()


def _html_confirmation_status(response: httpx.Response) -> str:
    """Recognize a 2xx HTML confirmation page without trusting its markup.

    Real boards answer a successful POST with an HTML confirmation instead of
    the JSON status used by controlled tests. Only a small allowlist of
    confirmation phrases counts, and only for a 2xx response: anything else
    stays unknown so the attempt is never marked ``SUBMITTED`` on a guess.
    """
    content_type = str(response.headers.get("content-type", "")).casefold()
    if "html" not in content_type:
        return ""
    try:
        text = response.text.casefold()
    except (UnicodeDecodeError, ValueError):
        return ""
    if any(marker in text for marker in _CONFIRMATION_MARKERS):
        return "submitted"
    return ""
