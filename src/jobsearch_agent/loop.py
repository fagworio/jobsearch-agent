"""O loop unico de candidatura (JSA-LOOP-001).

Um objetivo de produto: **dada uma vaga, chegar a um desfecho comprovado.** O
loop coordena as pecas que ja existiam — material, browser, orquestrador de
inspecao/preenchimento, coordenador de submissao, dominio — e nao reimplementa
nenhuma delas.

Ele NAO conhece ATS: nada de `if greenhouse`, seletor, marcador de desafio ou
corpo de POST. Adapter, sessao, material e politica de escrita entram por
`LoopRuntime`, injetados. Producao monta o runtime a partir de `Settings`
(`pipeline.loop_runtime`); o E2E monta um runtime sintetico. Sem `if testing`.

Os conceitos de fase (inspecionar, resolver, preencher, avancar, submeter,
observar) sao INTERNOS: eles nao viram `ApplicationState`, porque o enum
representa decisoes de dominio, nao etapas de implementacao.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol

from .application import TRANSITIONS, ApplicationService, evaluate_safety_gate
from .ats import ATSAdapter
from .coordinator import SubmissionCoordinator, SubmissionResult
from .models import (
    Application,
    ApplicationContext,
    ApplicationPolicy,
    ApplicationState,
    CandidatePreferences,
    CareerProfile,
    Job,
    to_dict,
)
from .orchestrator import LiveApplicationOrchestrator, LiveApplicationResult
from .persistence import Database
from .qa import AnswerKnowledgeBase
from .submission import compute_answers_fingerprint

#: Estados em que o loop PARA e nao ha nada mais a fazer sozinho.
TERMINAL_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SUBMITTED,
        ApplicationState.SUBMIT_UNKNOWN,
        ApplicationState.REJECTED,
        ApplicationState.POLICY_BLOCKED,
        ApplicationState.UNSUPPORTED_FORM,
    }
)

#: Estados em que nada mais pode ser enviado automaticamente: reenviar violaria o
#: exatamente-uma-vez. O loop para ANTES de preparar material e ANTES do browser.
NO_RESEND_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SUBMITTED,
        ApplicationState.SUBMIT_UNKNOWN,
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION,
    }
)

#: Estados que exigem intervencao humana ou contexto novo (nao terminais).
RECOVERABLE_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.NEEDS_ANSWER,
        ApplicationState.NEEDS_ARTIFACT,
        ApplicationState.NEEDS_LOGIN,
        ApplicationState.NEEDS_MFA,
        ApplicationState.NEEDS_CAPTCHA,
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
        ApplicationState.HANDOFF_IN_PROGRESS,
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION,
    }
)


class LoopError(ValueError):
    """O loop nao pode rodar para este job. Nenhum estado muda."""


class LoopPhase(StrEnum):
    """Fases internas. Nao persistidas como estado de dominio."""

    PREPARE = "prepare"
    INSPECT = "inspect"
    RESOLVE = "resolve"
    FILL = "fill"
    ADVANCE = "advance"
    SUBMIT = "submit"
    OBSERVE = "observe"


@dataclass(frozen=True)
class PreparedMaterial:
    """O material que vai para o formulario, com o hash que o identifica."""

    resume_path: str
    resume_sha256: str
    artifact_root: str = ""
    validation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplicationLoopResult:
    """Contrato final do loop: o mesmo em qualquer caminho de saida, sem PII."""

    application_id: str
    job_id: str
    state: ApplicationState
    status: str
    provider: str = ""
    cycles: int = 0
    steps_completed: int = 0
    questions_answered: int = 0
    unanswered_required: tuple[str, ...] = ()
    resume_sha256: str = ""
    form_fingerprint: str = ""
    answers_fingerprint: str = ""
    submission_attempted: bool = False
    submission_writes: int = 0
    terminal: bool = False
    requires_action: str = ""
    reason: str = ""
    phases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {key: to_dict(value) for key, value in self.__dict__.items()}


class MaterialPreparer(Protocol):
    def __call__(self, job: Job) -> PreparedMaterial: ...


#: Constroi a politica de escrita para (provider, application_id, intent_id).
PolicyFactory = Callable[[str, str, str], Any]


@dataclass(frozen=True)
class LoopRuntime:
    """Tudo que o loop precisa do mundo, injetado. Zero conhecimento de provider."""

    adapter_for: Callable[[Job], ATSAdapter]
    form_url: Callable[[Job, ATSAdapter], str]
    profile: CareerProfile
    preferences: CandidatePreferences
    answers: AnswerKnowledgeBase
    prepare: MaterialPreparer
    #: Abre e JA INICIA a sessao; o loop fecha no `finally`.
    open_session: Callable[[Job, ATSAdapter], Any]
    #: Ausente = politica declarada pelo provider (`LiveNetworkPolicy.for_submission`).
    #: A politica continua sendo validada contra a intent em `begin_submission`:
    #: injetar a CONSTRUCAO dela nao afrouxa a boundary.
    policy_for: PolicyFactory | None = None
    allow_insecure_destination: bool = False
    max_cycles: int = 5
    allow_advance: bool = True
    submission_timeout: float = 45.0


class ApplicationLoop:
    """`job_id` entra, um `ApplicationLoopResult` sai."""

    def __init__(
        self,
        database: Database,
        runtime: LoopRuntime,
        *,
        coordinator: SubmissionCoordinator | None = None,
    ):
        self.database = database
        self.runtime = runtime
        self.coordinator = coordinator or SubmissionCoordinator(
            database, timeout_seconds=runtime.submission_timeout
        )

    # -- entrada ---------------------------------------------------------------

    def run(self, job_id: str, *, submit: bool = True) -> ApplicationLoopResult:
        job = self.database.get_job(job_id)
        if job is None:
            raise LoopError(f"job not found: {job_id}")
        service = ApplicationService(self.database)
        application = service.create_for_job(job_id)

        # Reenvio e a unica coisa que NUNCA pode acontecer por acidente: se o
        # desfecho ja existe (ou e desconhecido), o loop para aqui — sem preparar
        # material, sem abrir browser e sem POST.
        blocked = self._no_resend(application, job)
        if blocked is not None:
            return blocked

        phases: list[str] = [LoopPhase.PREPARE.value]
        material = self.runtime.prepare(job)
        application = self._record_materials(service, application, material)
        adapter = self.runtime.adapter_for(job)
        form_url = self.runtime.form_url(job, adapter)
        session: Any = None
        try:
            session = self.runtime.open_session(job, adapter)
            phases.append(LoopPhase.INSPECT.value)
            context = ApplicationContext(
                application_id=application.id,
                job_id=job.id,
                validation=dict(material.validation),
                policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "auto"}),
            )
            orchestrator = LiveApplicationOrchestrator(
                adapter,
                self.runtime.profile,
                self.runtime.preferences,
                self.runtime.answers,
                artifact_root=material.artifact_root,
                default_resume=material.resume_path,
                max_cycles=self.runtime.max_cycles,
                allow_advance=self.runtime.allow_advance,
            )
            live = orchestrator.run(session, context, form_url)
            phases.extend([LoopPhase.RESOLVE.value, LoopPhase.FILL.value])
            if live.advanced_steps:
                phases.append(LoopPhase.ADVANCE.value)

            # A decisao de dominio vem do Safety Gate que o orquestrador ja
            # rodou; ela e persistida ANTES de qualquer ramificacao. Sem isto o
            # caminho de falha nao deixava rastro: um formulario com pergunta sem
            # resposta ficava em MATERIALS_READY, e o loop nao tinha estado para
            # reportar.
            application = self._record_decision(service, application, live, context)
            if live.status != "FILLED_REVIEW_REQUIRED":
                # O orquestrador ja decidiu: preenchimento incompleto, formulario
                # nao suportado, loop de etapas detectado ou erro de browser.
                # Nao se forca a resolucao "de qualquer jeito".
                return self._without_submit(application, job, live, phases)

            form = live.form
            form_fingerprint = live.form_fingerprint
            answers_fingerprint = compute_answers_fingerprint(form)
            application = self._record_review(service, application, live)
            if not submit:
                return self._result(
                    application,
                    job,
                    status="READY_TO_SUBMIT",
                    live=live,
                    phases=phases,
                    resume_sha256=material.resume_sha256,
                    answers_fingerprint=answers_fingerprint,
                    terminal=False,
                    requires_action="submit",
                    reason="preenchimento concluido; envio nao autorizado nesta execucao",
                )

            phases.append(LoopPhase.SUBMIT.value)
            submission = self.coordinator.submit(
                application=application,
                job=job,
                form=form,
                session=session,
                provider=adapter.provider,
                destination=form_url,
                resume_sha256=material.resume_sha256,
                form_fingerprint=form_fingerprint,
                answers_fingerprint=answers_fingerprint,
                allow_insecure_destination=self.runtime.allow_insecure_destination,
                policy_factory=self.runtime.policy_for,
            )
            phases.append(LoopPhase.OBSERVE.value)
            return self._from_submission(
                application, job, live, submission, material, form_fingerprint, answers_fingerprint, phases
            )
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    # -- decisoes --------------------------------------------------------------

    def _no_resend(self, application: Application, job: Job) -> ApplicationLoopResult | None:
        if application.state not in NO_RESEND_STATES:
            return None
        # Nunca reenviar NAO e o mesmo que terminal: uma Application em
        # AWAITING_SUBMISSION_CONFIRMATION ainda pode chegar a SUBMITTED por
        # evidencia independente. O que ela nao pode e gerar outro POST.
        terminal = application.state in TERMINAL_STATES
        return ApplicationLoopResult(
            application_id=application.id,
            job_id=job.id,
            state=application.state,
            status="ALREADY_SUBMITTED" if application.state is ApplicationState.SUBMITTED else "NO_RESEND",
            terminal=terminal,
            requires_action="" if terminal else application.state.value,
            reason="a candidatura ja tem desfecho; nada foi aberto nem enviado",
        )

    def _from_submission(
        self,
        application: Application,
        job: Job,
        live: LiveApplicationResult,
        submission: SubmissionResult,
        material: PreparedMaterial,
        form_fingerprint: str,
        answers_fingerprint: str,
        phases: list[str],
    ) -> ApplicationLoopResult:
        state = self._state(application.id)
        terminal = state in TERMINAL_STATES
        return self._result(
            application,
            job,
            status=submission.status,
            live=live,
            phases=phases,
            resume_sha256=material.resume_sha256,
            answers_fingerprint=answers_fingerprint,
            terminal=terminal,
            requires_action="" if terminal else state.value,
            reason=submission.error,
            submission_attempted=True,
            submission_writes=submission.writes,
            form_fingerprint=form_fingerprint,
        )

    def _without_submit(
        self, application: Application, job: Job, live: LiveApplicationResult, phases: list[str]
    ) -> ApplicationLoopResult:
        state = self._state(application.id)
        recoverable = state in RECOVERABLE_STATES
        status = live.status
        if status == "LOOP_DETECTED":
            # Repetir o mesmo formulario nao e para tentar de novo: e defeito de fluxo.
            recoverable = False
        return self._result(
            application,
            job,
            status=status,
            live=live,
            phases=phases,
            terminal=not recoverable,
            requires_action=state.value if recoverable else "",
            reason=live.error or status,
        )

    def _result(
        self,
        application: Application,
        job: Job,
        *,
        status: str,
        live: LiveApplicationResult,
        phases: list[str],
        resume_sha256: str = "",
        answers_fingerprint: str = "",
        terminal: bool,
        requires_action: str = "",
        reason: str = "",
        submission_attempted: bool = False,
        submission_writes: int = 0,
        form_fingerprint: str = "",
    ) -> ApplicationLoopResult:
        form = live.form
        return ApplicationLoopResult(
            application_id=application.id,
            job_id=job.id,
            state=self._state(application.id),
            status=status,
            provider=str(live.provider or ""),
            cycles=len(live.cycles),
            steps_completed=live.advanced_steps,
            questions_answered=_answered(form),
            unanswered_required=tuple(item["question"] for item in _pending(form)),
            resume_sha256=resume_sha256,
            form_fingerprint=form_fingerprint or live.form_fingerprint,
            answers_fingerprint=answers_fingerprint,
            submission_attempted=submission_attempted,
            submission_writes=submission_writes,
            terminal=terminal,
            requires_action=requires_action,
            reason=reason,
            phases=tuple(phases),
        )

    # -- persistencia ----------------------------------------------------------

    def _record_materials(
        self, service: ApplicationService, application: Application, material: PreparedMaterial
    ) -> Application:
        if application.state is ApplicationState.DRAFT:
            application = service.transition(
                application.id, ApplicationState.PREPARING, "application_preparing"
            )
        if application.state is ApplicationState.PREPARING:
            application = service.transition(
                application.id, ApplicationState.MATERIALS_READY, "materials_ready"
            )
        application.context["resume_sha256"] = material.resume_sha256
        self.database.save_application(application)
        return application

    def _record_decision(
        self,
        service: ApplicationService,
        application: Application,
        live: LiveApplicationResult,
        context: ApplicationContext,
    ) -> Application:
        """Persiste a decisao do Safety Gate — inclusive quando ela nao e sucesso."""
        readiness = live.readiness or evaluate_safety_gate(context)
        application.context["readiness"] = to_dict(readiness)
        self.database.save_application(application)
        decision = readiness.decision
        if decision is not application.state and decision in TRANSITIONS[application.state]:
            application = service.transition(
                application.id,
                decision,
                "live_fill_safety_gate",
                {"decision": decision.value, "blockers": list(readiness.blockers)},
            )
        return application

    def _record_review(
        self,
        service: ApplicationService,
        application: Application,
        live: LiveApplicationResult,
    ) -> Application:
        """Registra que o preenchimento chegou a superficie de revisao."""
        if application.state is ApplicationState.MATERIALS_READY:
            application = service.transition(
                application.id,
                ApplicationState.READY_FOR_REVIEW,
                "live_fill_materials_ready",
                {"network_access": "browser_guarded", "submission_attempted": False},
            )
        if application.state in {ApplicationState.READY_FOR_REVIEW, ApplicationState.READY_TO_APPLY}:
            application = service.transition(
                application.id,
                ApplicationState.REVIEW_REACHED,
                "live_fill_review_reached",
                {"network_access": "browser_guarded", "submission_attempted": False},
            )
        return application

    def _state(self, application_id: str) -> ApplicationState:
        application = self.database.get_application(application_id)
        if application is None:  # pragma: no cover - defensivo
            raise LoopError(f"application not found: {application_id}")
        return application.state


def _is_answered(field: Any) -> bool:
    """Um campo esta respondido por valor, por resposta OU por artefato anexado.

    O campo de curriculo nao tem `value` nem `answer`: o que ele tem e
    `attachment_path`. Sem contar o anexo, um formulario completo reportava
    "Resume" como pergunta sem resposta — e o relatorio do loop mentia sobre o
    unico campo que o proprio loop acabou de preencher.
    """
    if field.value not in (None, ""):
        return True
    if field.answer is not None and field.answer.answer:
        return True
    return bool(str(getattr(field, "attachment_path", "") or "").strip())


def _pending(form: Any) -> list[dict[str, str]]:
    """Perguntas obrigatorias sem resposta. Vazio quando o formulario esta completo."""
    if form is None:
        return []
    pending: list[dict[str, str]] = []
    for field in form.fields:
        if not field.required or _is_answered(field):
            continue
        pending.append({"key": field.key, "question": (field.label or field.key).strip()})
    return pending


def _answered(form: Any) -> int:
    if form is None:
        return 0
    return sum(1 for field in form.fields if _is_answered(field))
