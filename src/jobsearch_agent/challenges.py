"""Anti-corruption layer entre o jobsearch-agent e o challenge-guard (JSA-CG-002).

Este e o UNICO arquivo do projeto autorizado a importar `challenge_guard`. Se
outro modulo precisar de algo anti-bot, ele pede aqui — e a independencia do
dominio (Application, Job, Resume, SubmissionIntent, ATS) fica preservada.

A separacao de responsabilidades:

    challenge-guard   "ha challenge, e qual o estado dele?"
    submission        "houve escrita, e qual foi o resultado?"
    dominio           "qual ApplicationState corresponde a combinacao?"

O guard nunca decide submissao, e o executor nunca interpreta anti-bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from challenge_guard import (
    ChallengeDecisionStatus,
    ChallengeObservation,
    ChallengePhase,
    ChallengePolicy,
    ChallengeProvider,
    ChallengeSessionTracker,
    DOMObserver,
    FrameObserver,
    NetworkObserver,
    ResponseObserver,
    merge,
    redact,
    requirements_for,
)
from challenge_guard.providers import profile_for as challenge_profile_for
from challenge_guard.browser import PlaywrightChallengeAdapter
from challenge_guard.observers import ResponseRecord

from .browser import ChallengeRuntimePermission
from .models import ApplicationState

#: Passos do fluxo do host traduzidos para o vocabulario do guard.
_PHASE_BY_STEP: dict[str, ChallengePhase] = {
    "page_load": ChallengePhase.PAGE_LOAD,
    "form_discovery": ChallengePhase.FORM_DISCOVERY,
    "form_fill": ChallengePhase.FORM_FILL,
    "pre_submit": ChallengePhase.PRE_SUBMIT,
    "submitting": ChallengePhase.SUBMITTING,
    "post_submit": ChallengePhase.POST_SUBMIT,
}

#: Decisoes do guard -> ApplicationState. `None` significa "nao mexer no estado".
_DECISION_STATE: dict[str, str | None] = {
    ChallengeDecisionStatus.NONE.value: None,
    ChallengeDecisionStatus.OBSERVE.value: None,
    ChallengeDecisionStatus.NEEDS_HUMAN.value: ApplicationState.NEEDS_CAPTCHA.value,
    ChallengeDecisionStatus.PROVIDER_REJECTED.value: ApplicationState.NEEDS_HUMAN_CAPTCHA.value,
    ChallengeDecisionStatus.RESOLVED_EXTERNALLY.value: None,
}


@dataclass(frozen=True)
class ChallengeOutcome:
    """O que o executor precisa saber — sem tipos do challenge-guard."""

    decision: str
    reason_token: str
    provider: str = "unknown"
    human_required: bool = False
    application_state: str | None = None
    evidence: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.evidence is None:
            object.__setattr__(self, "evidence", {})


def application_state_for(decision: str, *, browser_write_sent: bool) -> str | None:
    """Mapeia a decisao do guard para o dominio (JSA-CG-003).

    `UNKNOWN` depende de ter havido escrita: sem escrita nada foi recusado, e
    inventar falha de candidatura seria afirmar o falso; com escrita, o desfecho
    e genuinamente desconhecido e vira `SUBMIT_UNKNOWN`, que preserva o
    exatamente-uma-vez (nunca reenviar sozinho).
    """
    if decision != ChallengeDecisionStatus.UNKNOWN.value:
        return _DECISION_STATE.get(decision)
    return ApplicationState.SUBMIT_UNKNOWN.value if browser_write_sent else None


class JobsearchChallengeAdapter:
    """Compoe observadores, sessao e policy num veredito para o executor."""

    def __init__(self, *, confidence_threshold: float = 0.5) -> None:
        self._browser = PlaywrightChallengeAdapter()
        self._policy = ChallengePolicy(confidence_threshold=confidence_threshold)
        self._tracker = ChallengeSessionTracker()
        self._session = None

    # -- ciclo de vida ---------------------------------------------------------

    def attach(self, page: Any) -> None:
        self._browser.attach(page)

    def detach(self) -> None:
        self._browser.detach()

    @property
    def attached(self) -> bool:
        return self._browser.attached

    def reset(self) -> None:
        """Nova tentativa nao herda sessao nem observacao transitoria."""
        self._browser.reset()
        self._session = None

    # -- observacao ------------------------------------------------------------

    def observe(
        self,
        *,
        step: str = "pre_submit",
        http_status: int | None = None,
        page_errors: Iterable[str] = (),
        browser_write_sent: bool = False,
        submission_confirmed: bool = False,
    ) -> ChallengeOutcome:
        phase = _PHASE_BY_STEP.get(step, ChallengePhase.PRE_SUBMIT)
        dom = DOMObserver().observe(self._browser.collect_dom())
        frames = FrameObserver().observe(self._browser.collect_frames())
        network = NetworkObserver().observe(self._browser.collect_network())

        # O texto que a propria pagina mostra apos o clique e a evidencia de
        # recusa que existe no browser. O observer converte em conceito; o texto
        # nao e guardado.
        records = [ResponseRecord(status=http_status, text=str(text)) for text in page_errors]
        records += [ResponseRecord(status=item.status, text=item.text) for item in self._browser.collect_responses()]
        response = ResponseObserver().observe(records)

        merged = merge([dom, frames, network, response])
        observation = ChallengeObservation(
            detected=merged.detected,
            phase=phase,
            provider=merged.provider,
            challenge_type=merged.challenge_type,
            visible=merged.detected,
            browser_write_sent=browser_write_sent,
            http_status=http_status if isinstance(http_status, int) else None,
            dom_signals=dom.structure,
            frame_signals=frames.structure,
            signals=merged.signals,
            confidence=merged.confidence,
        )

        if merged.detected and self._session is None:
            self._session = self._tracker.start(observation)
        elif self._session is not None:
            self._tracker.observe(self._session, observation)

        decision = self._policy.decide(
            observation, self._session, submission_confirmed=submission_confirmed
        )
        evidence = redact(session=self._session, observation=observation, decision=decision)
        return ChallengeOutcome(
            decision=decision.status.value,
            reason_token=decision.reason_token,
            provider=decision.provider.value,
            human_required=decision.human_required,
            application_state=application_state_for(
                decision.status.value, browser_write_sent=browser_write_sent
            ),
            evidence=evidence.to_dict(),
        )

    # -- requisitos de rede ----------------------------------------------------

    def runtime_permissions(
        self, providers: Sequence[str] | None = None
    ) -> list[ChallengeRuntimePermission]:
        """Converte requisitos do guard em permissoes do guard de rede.

        Ausencia de caminho declarado NAO vira `^/.*$`: sem caminho conhecido o
        host simplesmente nao autoriza. "Nao sei" nunca pode virar "pode tudo".

        `providers=None` arma a uniao dos provedores declarados. E deliberado: o
        widget precisa carregar para que a identificacao seja possivel, entao
        armar so depois de observar seria tarde. Cada permissao continua
        limitada a origem e ao caminho declarados, com orcamento proprio — nao
        ha concessao ampla.
        """
        names = list(providers) if providers is not None else [item.value for item in ChallengeProvider]
        permissions: list[ChallengeRuntimePermission] = []
        for name in names:
            try:
                provider = ChallengeProvider(name)
            except ValueError:
                continue
            for requirement in requirements_for(provider):
                if not requirement.path_patterns:
                    continue
                # Padroes ancorados viram uma alternacao ancorada: um permit por
                # origem e metodo, ainda estritamente com escopo de caminho.
                joined = "^(?:" + "|".join(
                    pattern.lstrip("^") for pattern in requirement.path_patterns
                ) + ")"
                for origin in requirement.origins:
                    for method in requirement.methods:
                        permissions.append(
                            ChallengeRuntimePermission(
                                provider=provider.value,
                                origin=origin,
                                path_pattern=joined,
                                method=method,
                                max_requests=requirement.max_requests,
                            )
                        )
        return permissions

    def runtime_read_hosts(self, providers: Sequence[str] | None = None) -> set[str]:
        """Hosts que a pagina precisa ALCANCAR (leitura) para o widget carregar.

        Sem eles o desafio nem aparece e "resolver manualmente" fica impossivel —
        o humano veria um botao que nao faz nada. Vem do challenge-guard porque e
        conhecimento do provedor de desafio, nao do ATS: um board que troque de
        anti-bot nao deve exigir edicao em `providers.py`.
        """
        names = list(providers) if providers is not None else [item.value for item in ChallengeProvider]
        hosts: set[str] = set()
        for name in names:
            try:
                provider = ChallengeProvider(name)
            except ValueError:
                continue
            profile = challenge_profile_for(provider)
            if profile is None:
                continue
            hosts.update(profile.frame_hosts)
            hosts.update(profile.runtime_hosts)
            hosts.update(profile.widget_hosts)
        return hosts

    def tracked_session_status(self) -> str:
        return self._session.status.value if self._session is not None else ""
