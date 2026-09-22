"""Controlled Greenhouse submission executor.

This provider adapter owns HTTP behavior. It cannot bypass the domain
submission boundary: the service must authorize the intent, validate the live
network policy, and persist the attempt before this class sends a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import httpx

from .persistence import Database
from .submission import LiveNetworkPolicy, SubmissionBoundaryError, SubmissionService, SubmissionVerification


@dataclass(frozen=True)
class SubmissionExecutionResult:
    status: str
    attempt_id: str
    http_status: int | None = None
    error: str = ""


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
        payload: Mapping[str, str],
    ) -> SubmissionExecutionResult:
        intent = self.database.get_submission_intent(intent_id)
        if not intent:
            raise SubmissionBoundaryError(f"submission intent not found: {intent_id}")
        if intent.provider != self.provider:
            raise SubmissionBoundaryError(f"Greenhouse executor cannot handle provider: {intent.provider}")
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
        try:
            response = client.post(intent.destination, data=dict(payload), timeout=self.timeout)
        except httpx.TimeoutException:
            completed = service.record_result(attempt.id, SubmissionVerification.unknown("request timeout"))
            return SubmissionExecutionResult(completed.status, completed.id, error="request timeout")
        except httpx.TransportError:
            completed = service.record_result(attempt.id, SubmissionVerification.unknown("transport uncertainty"))
            return SubmissionExecutionResult(completed.status, completed.id, error="transport uncertainty")
        finally:
            if owned_client:
                client.close()

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
                return SubmissionExecutionResult(completed.status, completed.id, status_code)
            completed = service.record_result(attempt.id, SubmissionVerification.unknown("confirmation missing"))
            return SubmissionExecutionResult(completed.status, completed.id, status_code, "confirmation missing")

        if 300 <= status_code < 400:
            completed = service.record_result(
                attempt.id,
                SubmissionVerification.unknown("redirect without confirmation"),
            )
            return SubmissionExecutionResult(completed.status, completed.id, status_code, "redirect without confirmation")

        completed = service.record_result(
            attempt.id,
            SubmissionVerification.failed("provider rejected submission"),
        )
        return SubmissionExecutionResult(completed.status, completed.id, status_code, "provider rejected submission")

    @staticmethod
    def _provider_status(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        return str(body.get("status", "")).casefold().strip()
