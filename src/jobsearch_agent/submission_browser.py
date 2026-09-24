"""Submissão autorizada executada pela própria aplicação no browser.

Boards modernos publicam um ``submitPath`` e montam o pedido dentro do próprio
JavaScript, com tokens efêmeros que nenhum cliente HTTP externo reproduz — o que
explica os 400 do executor via httpx. Por isso o POST é feito pela página.

Aqui a escrita continua sob a Submission Boundary: a intent é validada, a
tentativa é persistida **antes** do clique e o ``NetworkWriteGuard`` é armado
para exatamente um POST na origem e no caminho autorizados. Nenhum token é
forjado, extraído para replay ou contornado.

Anti-bot NÃO é conhecido neste módulo. Marcadores, hosts, iframes e mensagens de
recusa vivem no ``challenge-guard`` e chegam aqui como `ChallengeOutcome` pela
ACL ``jobsearch_agent.challenges``. Os detalhes operacionais da descoberta
histórica (qual board monta o pedido como, e com quais tokens) ficam em
docs/adr, não no módulo de ATS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from .browser import AuthorizedWrite, BrowserSessionError, GuardedBrowserSession, dismiss_cookie_consent
from .challenges import ChallengeOutcome, JobsearchChallengeAdapter
from .models import ApplicationForm, now_iso
from .persistence import Database
from .providers import ProviderError, profile_for
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

# Conhecimento anti-bot (marcadores, hosts, iframes, 428, mensagens de recusa)
# NAO pertence a este modulo. Ele vem de `jobsearch_agent.challenges`, que e a
# unica ponte para o challenge-guard. Aqui so existe `ChallengeOutcome`.


class BrowserSubmitter:
    """Executa o POST de submissão pela própria aplicação, no browser.

    O comportamento varia por provider através de :mod:`providers`: rótulos do
    controle final, marcadores de confirmação e endpoint autorizado. Ashby
    carrega o formulário por POST na API, então é recusado aqui em vez de
    falhar de forma obscura.
    """

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
        try:
            profile = profile_for(intent.provider)
        except ProviderError as exc:
            raise SubmissionBoundaryError(str(exc)) from exc
        if profile.form_loaded_by_api_write:
            return BrowserSubmissionOutcome(
                "UNSUPPORTED_PROVIDER",
                evidence={"provider": profile.provider},
                error=(
                    f"{profile.provider}: o formulario e carregado por POST na API "
                    f"({profile.notes}), o que exige um modelo de autorizacao de "
                    "escrita na fase de inspecao; nao suportado ainda"
                ),
            )
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

        permits = [
            AuthorizedWrite(
                application_id=policy.application_id,
                submission_intent_id=intent_id,
                origin=policy.allowed_origin,
                path_pattern=policy.allowed_path_pattern,
                method=policy.allowed_method,
                max_writes=1,
            )
        ]
        session.arm_writes(permits)
        # Runtime do widget anti-bot: boundary PROPRIA, com orcamento independente
        # do de submissao. Os requisitos vem do challenge-guard via a ACL; este
        # modulo nao conhece host, caminho nem provider de desafio.
        challenges = JobsearchChallengeAdapter()
        challenges.attach(getattr(session, "page", None))
        session.arm_challenge_runtime(challenges.runtime_permissions())
        submit_writes_used = 0
        try:
            observed = self._click_and_observe(session, intent.destination, profile, challenges)
            guard = getattr(session, "network_guard", None)
            usage = getattr(guard, "authorized_write_usage", None) if guard is not None else None
            if usage:
                # Posicao 0 e sempre o permit da candidatura.
                submit_writes_used = int(usage[0][1])
            observed["authorized_writes_used"] = submit_writes_used
            # O veredito anti-bot vem da ACL, e precisa ser colhido ENQUANTO a
            # observacao esta anexada: `detach` descarta a pagina e os buffers, e
            # observar depois dele devolveria "nenhum challenge" — um falso
            # negativo silencioso em todo board com desafio.
            outcome = challenges.observe(
                step="post_submit",
                http_status=observed.get("http_status"),
                page_errors=observed.get("page_errors") or (),
                browser_write_sent=bool(submit_writes_used),
                submission_confirmed=bool(observed.get("confirmation_reached"))
                and 200 <= int(observed.get("http_status") or 0) < 300,
            )
        finally:
            session.disarm_authorized_write()
            session.disarm_challenge_runtime()
            challenges.detach()
        writes_used = submit_writes_used
        observed["blocked_writes"] = len(getattr(guard, "blocked_writes", []) or [])
        observed["captcha_challenge"] = bool(outcome.human_required)
        observed["challenge_decision"] = outcome.decision
        observed["challenge_reason_token"] = outcome.reason_token
        observed["challenge_provider"] = outcome.provider
        verification, status = self._classify(observed, writes_used, outcome)
        if verification is None:
            # Nenhuma escrita saiu: o desafio apareceu e ninguem o resolveu. Nao
            # houve tentativa de submissao, entao o registro nao pode afirmar
            # que o servidor recusou algo.
            completed = service.record_result(
                attempt.id,
                SubmissionVerification.failed(
                    "captcha challenge; no write left the browser",
                    reason_token="captcha_no_write",
                ),
            )
            return BrowserSubmissionOutcome(
                "NEEDS_CAPTCHA",
                completed.id,
                evidence=observed,
                error="CAPTCHA challenge: rerun with a visible browser and solve it manually",
            )
        completed = service.record_result(attempt.id, verification)
        if status == "NEEDS_HUMAN_CAPTCHA":
            return BrowserSubmissionOutcome(
                status,
                completed.id,
                observed.get("http_status"),
                observed,
                error=(
                    "anti-bot verification rejected a submission that was sent; "
                    "human handoff required (the agent will not disguise automation)"
                ),
            )
        return BrowserSubmissionOutcome(status, completed.id, observed.get("http_status"), observed)

    def _classify(self, observed: dict[str, Any], writes_used: int, outcome: ChallengeOutcome | None = None):
        status_code = observed.get("http_status")
        confirmed = bool(observed.get("confirmation_reached"))
        # Uma escrita confirmada com 2xx e o desfecho mais forte que existe: um
        # CAPTCHA que o humano acabou de resolver nao pode rebaixar uma
        # candidatura que o servidor aceitou.
        if writes_used and confirmed and 200 <= (status_code or 0) < 300:
            return (
                SubmissionVerification.confirmed(
                    "browser_response",
                    {"status_code": int(status_code), "provider_status": "confirmation_path"},
                ),
                "SUBMITTED",
            )
        decision = outcome.decision if outcome is not None else "none"
        if decision == "provider_rejected":
            # A submissao saiu e o provedor recusou a verificacao anti-bot. Nao e
            # "resolva o CAPTCHA e tente de novo" pelo agente: repetir nao muda o
            # veredito, e disfarcar sinais de automacao esta fora de escopo.
            return (
                SubmissionVerification.challenged(
                    "captcha_verification_failed",
                    http_status=status_code if isinstance(status_code, int) else None,
                    submit_write=bool(writes_used),
                    # Proveniencia observada: sem ela o handoff humano posterior
                    # nao saberia QUAL desafio recusou o envio nem em que sessao.
                    challenge={
                        "provider": outcome.provider,
                        "reason_token": outcome.reason_token,
                        "session_id": outcome.session_id,
                    },
                ),
                "NEEDS_HUMAN_CAPTCHA",
            )
        if decision == "needs_human":
            # Desafio bloqueando antes de qualquer escrita: nada foi enviado, e o
            # registro nao pode afirmar que o servidor recusou uma candidatura.
            return None, "NEEDS_CAPTCHA"
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

    def _click_and_observe(
        self,
        session: GuardedBrowserSession,
        destination: str,
        profile=None,
        challenges: JobsearchChallengeAdapter | None = None,
    ) -> dict[str, Any]:
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
            # O overlay do banner de cookies intercepta o clique no controle
            # final; dispensar antes evita que o envio nunca chegue ao botao.
            dismiss_cookie_consent(page)
            control = self._submit_control(page, getattr(profile, "submit_control_names", ()) or SUBMIT_CONTROL_NAMES)
            observed["submit_control"] = "Submit application" if control is not None else ""
            if control is None:
                observed["error"] = "no unique enabled submit control in the application form"
                return observed
            control.click()
            # Numa sessao sem janela nao ha quem resolva um desafio: reportar e
            # parar. Com janela visivel o agente espera, porque a mensagem
            # promete que o humano pode resolver — abortar no instante em que o
            # desafio aparece tornava essa promessa impossivel de cumprir.
            human_can_solve = not bool(getattr(session, "headless", True))
            deadline = time.monotonic() + self.timeout_seconds
            last_challenge_check = 0.0
            while time.monotonic() < deadline:
                if responses:
                    break
                if self._confirmation_visible(page, getattr(profile, "confirmation_markers", ()) or CONFIRMATION_MARKERS):
                    break
                # Sem janela visivel nao ha quem resolva: se o challenge exige
                # humano, esperar a deadline inteira seria desperdicio. A
                # pergunta e feita a ACL, nao a um seletor de iframe — este
                # modulo nao tem mais como saber o que e um CAPTCHA.
                if not human_can_solve and challenges is not None:
                    now = time.monotonic()
                    if now - last_challenge_check >= 1.0:
                        last_challenge_check = now
                        if challenges.observe(step="submitting").human_required:
                            break
                page.wait_for_timeout(250)
            if responses:
                observed["http_status"] = responses[-1][1]
                observed["response_url"] = responses[-1][0]
            observed["confirmation_reached"] = self._confirmation_visible(
                page, getattr(profile, "confirmation_markers", ()) or CONFIRMATION_MARKERS
            )
            observed["final_url"] = str(getattr(page, "url", ""))
            observed["page_errors"] = self._page_errors(page)
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:  # pragma: no cover - defensivo
                pass
        return observed

    @staticmethod
    def _submit_control(page: Any, names: tuple[str, ...] = SUBMIT_CONTROL_NAMES) -> Any | None:
        """Localiza o controle final: um unico botao, visivel e habilitado."""
        for name in names:
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
    def _page_errors(page: Any) -> list[str]:
        """Mensagens de validacao que a propria pagina mostra apos o clique.

        Sem isso, "nenhuma escrita saiu" nao diz se o formulario foi recusado
        pelo cliente ou se o handler sequer rodou.
        """
        messages: list[str] = []
        for selector in ("[role=alert]", "[aria-invalid=true]", ".error", "[class*=error]"):
            try:
                found = page.locator(selector)
            except Exception:  # pragma: no cover - defensivo
                continue
            for index in range(min(found.count(), 12)):
                try:
                    item = found.nth(index)
                    if not item.is_visible():
                        continue
                    text = " ".join((item.inner_text() or "").split())
                    if text and text not in messages:
                        messages.append(text[:160])
                except Exception:  # pragma: no cover - defensivo
                    continue
        return messages[:12]

    @staticmethod
    def _confirmation_visible(page: Any, markers: tuple[str, ...] = CONFIRMATION_MARKERS) -> bool:
        url = str(getattr(page, "url", "")).casefold()
        if "/confirmation" in url:
            return True
        try:
            text = (page.inner_text("body") or "").casefold()
        except Exception:  # pragma: no cover - defensivo
            return False
        return any(marker in text for marker in markers)


#: Nome anterior, mantido para compatibilidade de importacao.
GreenhouseBrowserSubmitter = BrowserSubmitter
