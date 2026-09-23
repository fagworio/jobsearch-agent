"""Submissão autorizada executada pela própria aplicação no browser.

O board moderno do Greenhouse publica um ``submitPath`` e monta o pedido dentro
do seu próprio JavaScript: ele envia ``application/json`` com
``g-recaptcha-enterprise-token`` (obtido por ``performAssessment()``),
``request_token``, ``csrfToken`` e ``fingerprint``. Nada disso é reproduzível
por um cliente HTTP externo — o que explica o 400 do executor via httpx.

Aqui a escrita continua sob a Submission Boundary: a intent é validada, a
tentativa é persistida **antes** do clique e o ``NetworkWriteGuard`` é armado
para exatamente um POST na origem e no caminho autorizados. Nenhum token é
forjado, extraído para replay ou contornado: se a página apresentar um desafio
de CAPTCHA, a execução para e reporta ``NEEDS_CAPTCHA``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from .browser import AuthorizedWrite, BrowserSessionError, GuardedBrowserSession
from .models import ApplicationForm, now_iso
from .persistence import Database
from .submission import (
    LiveNetworkPolicy,
    SubmissionBoundaryError,
    SubmissionService,
    SubmissionVerification,
)


class BrowserSubmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserSubmissionOutcome:
    status: str
    attempt_id: str = ""
    http_status: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""


#: Rotulos aceitos para o controle final de envio, do mais para o menos especifico.
SUBMIT_CONTROL_NAMES = ("Submit application", "Submit Application", "Submit")

#: Marcadores de que a página chegou à confirmação.
CONFIRMATION_MARKERS = (
    "thank you for applying",
    "thanks for applying",
    "application submitted",
    "application has been submitted",
    "we received your application",
    "your application was submitted",
)

#: Marcadores de desafio de CAPTCHA que exigem intervenção humana.
CAPTCHA_MARKERS = ("recaptcha challenge", "g-recaptcha", "captcha")


class GreenhouseBrowserSubmitter:
    """Executa o POST de submissão pela aplicação Greenhouse no browser."""

    provider = "greenhouse"

    def __init__(self, database: Database, *, timeout_seconds: float = 45.0):
        if timeout_seconds <= 0:
            raise ValueError("submission timeout must be positive")
        self.database = database
        self.timeout_seconds = timeout_seconds

    def submit(
        self,
        session: GuardedBrowserSession,
        intent_id: str,
        *,
        current_form_fingerprint: str,
        current_resume_sha256: str,
        current_answers_fingerprint: str,
        policy: LiveNetworkPolicy,
    ) -> BrowserSubmissionOutcome:
        intent = self.database.get_submission_intent(intent_id)
        if not intent:
            raise SubmissionBoundaryError(f"submission intent not found: {intent_id}")
        if intent.provider != self.provider:
            raise SubmissionBoundaryError(f"Greenhouse browser submitter cannot handle provider: {intent.provider}")
        page = getattr(session, "page", None)
        if page is None:
            raise BrowserSessionError("browser submission requires a live page")

        service = SubmissionService(self.database)
        # Valida intent, politica, destino, fingerprints e duplicidade, e
        # persiste a tentativa ANTES de qualquer escrita de rede.
        attempt = service.begin_submission(
            intent_id,
            current_form_fingerprint=current_form_fingerprint,
            current_resume_sha256=current_resume_sha256,
            current_answers_fingerprint=current_answers_fingerprint,
            policy=policy,
            method=policy.allowed_method,
            url=intent.destination,
        )

        session.arm_authorized_write(
            AuthorizedWrite(
                application_id=policy.application_id,
                submission_intent_id=intent_id,
                origin=policy.allowed_origin,
                path_pattern=policy.allowed_path_pattern,
                method=policy.allowed_method,
                max_writes=1,
            )
        )
        try:
            observed = self._click_and_observe(session, intent.destination)
        finally:
            session.disarm_authorized_write()

        guard = getattr(session, "network_guard", None)
        writes_used = int(getattr(guard, "authorized_writes_used", 0) or 0)
        observed["authorized_writes_used"] = writes_used
        observed["blocked_writes"] = len(getattr(guard, "blocked_writes", []) or [])

        verification, status = self._classify(observed, writes_used)
        if verification is None:
            # Nada saiu e a página pediu intervenção: nao consome resultado
            # terminal. A tentativa fica registrada como falha definitiva
            # porque o guard prova que nenhuma escrita ocorreu.
            completed = service.record_result(
                attempt.id,
                SubmissionVerification.failed("captcha challenge; no write left the browser"),
            )
            return BrowserSubmissionOutcome(
                "NEEDS_CAPTCHA",
                completed.id,
                evidence=observed,
                error="CAPTCHA challenge: rerun with a visible browser and solve it manually",
            )
        completed = service.record_result(attempt.id, verification)
        return BrowserSubmissionOutcome(status, completed.id, observed.get("http_status"), observed)

    def _classify(self, observed: dict[str, Any], writes_used: int):
        if observed.get("captcha_challenge"):
            return None, "NEEDS_CAPTCHA"
        status_code = observed.get("http_status")
        confirmed = bool(observed.get("confirmation_reached"))
        if writes_used == 0:
            return (
                SubmissionVerification.failed("submit control did not produce a write"),
                "SUBMIT_FAILED",
            )
        if status_code is not None and status_code >= 400:
            return (
                SubmissionVerification.failed(f"provider responded {status_code}"),
                "SUBMIT_FAILED",
            )
        if 200 <= (status_code or 0) < 300 and confirmed:
            return (
                SubmissionVerification.confirmed(
                    "browser_response",
                    {"status_code": int(status_code), "provider_status": "confirmation_path"},
                ),
                "SUBMITTED",
            )
        return (
            SubmissionVerification.unknown("write left the browser without a definitive confirmation"),
            "SUBMIT_UNKNOWN",
        )

    def _click_and_observe(self, session: GuardedBrowserSession, destination: str) -> dict[str, Any]:
        page = session.page
        observed: dict[str, Any] = {"destination": destination, "started_at": now_iso()}
        responses: list[tuple[str, int]] = []

        def _on_response(response: Any) -> None:
            try:
                url = str(getattr(response, "url", ""))
                if url.rstrip("/") == destination.rstrip("/"):
                    responses.append((url, int(getattr(response, "status", 0))))
            except Exception:  # pragma: no cover - defensivo
                return

        page.on("response", _on_response)
        try:
            control = self._submit_control(page)
            observed["submit_control"] = "Submit application" if control is not None else ""
            if control is None:
                observed["captcha_challenge"] = self._captcha_visible(page)
                observed["error"] = "no unique enabled submit control in the application form"
                return observed
            control.click()
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                if responses:
                    break
                if self._confirmation_visible(page):
                    break
                if self._captcha_visible(page):
                    break
                page.wait_for_timeout(250)
            if responses:
                observed["http_status"] = responses[-1][1]
                observed["response_url"] = responses[-1][0]
            observed["confirmation_reached"] = self._confirmation_visible(page)
            observed["captcha_challenge"] = self._captcha_visible(page)
            observed["final_url"] = str(getattr(page, "url", ""))
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:  # pragma: no cover - defensivo
                pass
        return observed

    @staticmethod
    def _submit_control(page: Any) -> Any | None:
        """Localiza o controle final: um unico botao, visivel e habilitado."""
        for name in SUBMIT_CONTROL_NAMES:
            candidates = page.get_by_role("button", name=name, exact=True)
            visible = [
                candidates.nth(index)
                for index in range(candidates.count())
                if candidates.nth(index).is_visible() and candidates.nth(index).is_enabled()
            ]
            if len(visible) == 1:
                return visible[0]
        return None

    @staticmethod
    def _confirmation_visible(page: Any) -> bool:
        url = str(getattr(page, "url", "")).casefold()
        if "/confirmation" in url:
            return True
        try:
            text = (page.inner_text("body") or "").casefold()
        except Exception:  # pragma: no cover - defensivo
            return False
        return any(marker in text for marker in CONFIRMATION_MARKERS)

    @staticmethod
    def _captcha_visible(page: Any) -> bool:
        """Desafio que exige humano. Nunca tentamos resolver ou contornar."""
        try:
            frames = list(getattr(page, "frames", []) or [])
            for frame in frames:
                url = str(getattr(frame, "url", "")).casefold()
                if "recaptcha" in url and "challenge" in url:
                    return True
            for marker in CAPTCHA_MARKERS:
                if page.locator(f'iframe[src*="{marker}"]').count() > 0:
                    return True
        except Exception:  # pragma: no cover - defensivo
            return False
        return False
