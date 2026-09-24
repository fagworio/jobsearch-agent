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

# Somente API PUBLICA: nada de `challenge_guard.observers`,
# `challenge_guard.providers.registry` nem `challenge_guard.browser`. Assim a
# biblioteca pode ser refatorada por dentro sem quebrar este consumidor.
from challenge_guard import (
    ChallengeDecision,
    ChallengeDecisionStatus,
    ChallengeMonitor,
    ChallengePhase,
    ChallengeProvider,
    UnsafeHandoff,
    build_handoff,
    redact,
    requirements_for,
    runtime_read_hosts,
)

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
    handoff: dict[str, Any] = None  # type: ignore[assignment]
    session_id: str = ""

    def __post_init__(self) -> None:
        if self.evidence is None:
            object.__setattr__(self, "evidence", {})
        if self.handoff is None:
            object.__setattr__(self, "handoff", {})


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


def recorded_handoff(
    *,
    provider: str,
    reason_token: str,
    session_id: str = "",
    page_url: str = "",
) -> dict[str, Any]:
    """Handoff neutro reconstruido de uma recusa JA registrada (JSA-CG-016).

    Quando o humano pede o pacote, o processo que observou o desafio ja morreu:
    a observacao precisa ser reconstruivel a partir do que ficou gravado na
    tentativa. As INSTRUCOES continuam vindo da biblioteca — o host nao passa a
    manter uma copia propria do texto, e um motivo fora do conjunto fechado e
    recusado por ela, nao por uma checagem local que poderia divergir.

    `page_url` e opcional porque o guard recusa URL com query: e melhor perder o
    atalho do que gravar um identificador de sessao dentro de um handoff. Sem
    URL, o pacote aponta para a pagina que o proprio dominio ja conhece.
    """
    try:
        observed_provider = ChallengeProvider(provider)
    except ValueError:
        observed_provider = ChallengeProvider.UNKNOWN
    try:
        decision = ChallengeDecision(
            status=ChallengeDecisionStatus.PROVIDER_REJECTED,
            provider=observed_provider,
            reason_token=reason_token,
            human_required=True,
            retry_allowed=False,
            confidence=1.0,
        )
    except ValueError:
        # Motivo fora do conjunto fechado da biblioteca: nao ha handoff a montar.
        return {}
    try:
        handoff = build_handoff(decision, session_id=session_id, page_url=page_url)
    except UnsafeHandoff:
        handoff = build_handoff(decision, session_id=session_id)
    return handoff.to_dict() if handoff is not None else {}


class JobsearchChallengeAdapter:
    """Compoe observadores, sessao e policy num veredito para o executor."""

    def __init__(self, *, confidence_threshold: float = 0.5) -> None:
        self._monitor = ChallengeMonitor(confidence_threshold=confidence_threshold)

    # -- ciclo de vida ---------------------------------------------------------

    def attach(self, page: Any) -> None:
        if page is not None:
            self._monitor.attach(page)

    def detach(self) -> None:
        self._monitor.detach()

    @property
    def attached(self) -> bool:
        return self._monitor.attached

    def reset(self) -> None:
        """Nova tentativa nao herda sessao nem observacao transitoria."""
        self._monitor.reset()

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
        """Um veredito anti-bot, do monitor publico do challenge-guard.

        A composicao (DOM, frames, rede, resposta) e responsabilidade da
        biblioteca: este modulo nao monta observadores nem interpreta nada.
        """
        observation = self._monitor.observe(
            phase=_PHASE_BY_STEP.get(step, ChallengePhase.PRE_SUBMIT),
            http_status=http_status,
            response_texts=page_errors,
            browser_write_sent=browser_write_sent,
            submission_confirmed=submission_confirmed,
        )
        decision = self._monitor.decide(submission_confirmed=submission_confirmed)
        handoff = self._monitor.handoff()
        # A evidencia persistivel e produzida pelo redactor da biblioteca: o
        # consumidor nao monta esse objeto sozinho.
        evidence = redact(
            session=self._monitor.session,
            observation=observation,
            decision=decision,
        ).to_dict()
        return ChallengeOutcome(
            decision=decision.status.value,
            reason_token=decision.reason_token,
            provider=decision.provider.value,
            human_required=decision.human_required,
            application_state=application_state_for(
                decision.status.value, browser_write_sent=browser_write_sent
            ),
            evidence=evidence,
            handoff=handoff.to_dict() if handoff is not None else {},
            session_id=observation.session_id,
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
        """Hosts que a pagina precisa ALCANCAR para o widget carregar.

        Vem do challenge-guard porque e conhecimento do provedor de desafio, nao
        do ATS: um board que troque de anti-bot nao deve exigir edicao em
        `providers.py`.
        """
        names = None if providers is None else list(providers)
        return runtime_read_hosts(names)

    def pre_detection_runtime_permissions(self) -> list[ChallengeRuntimePermission]:
        """`PreDetectionChallengeRuntimePolicy`.

        Existe um bootstrap paradox real: para identificar o provider e preciso
        que o trafego dele carregue, mas so se sabe qual e o provider depois de
        observar. A resposta NAO e liberar genericamente.

        Antes da identificacao, o host arma a UNIAO FINITA dos requisitos de
        runtime dos providers explicitamente suportados. Isso nao e autorizacao
        generica de escrita: cada requisicao continua tendo de satisfazer um
        requirement provider-specific, limitado por origem, metodo, caminho e
        orcamento. Provider desconhecido nao amplia a uniao, e `GENERIC` nao
        declara requisito — logo nao adiciona permit nenhum.
        """
        return self.runtime_permissions()

    def tracked_session_status(self) -> str:
        return self._monitor.session.status.value if self._monitor.session is not None else ""
