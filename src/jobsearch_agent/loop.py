"""O loop unico de candidatura (JSA-LOOP-001 / JSA-LOOP-002).

Um objetivo de produto: **dada uma vaga, chegar a um desfecho comprovado.** O
loop coordena as pecas que ja existiam — material, browser, orquestrador de
inspecao/preenchimento, resolvedor de perguntas, coordenador de submissao,
dominio — e nao reimplementa nenhuma delas.

Ele NAO conhece ATS: nada de `if greenhouse`, seletor, marcador de desafio ou
corpo de POST. Adapter, sessao, material, gerador de resposta e politica de
escrita entram por `LoopRuntime`, injetados. Producao monta o runtime a partir de
`Settings` (`pipeline.loop_runtime`); o E2E monta um sintetico. Sem `if testing`.

**Multi-step (JSA-LOOP-002).** O `ApplicationForm` significa "o que esta no DOM
agora"; quem responde pela candidatura inteira e o `ApplicationJourney`, que
acumula cada etapa. A autorizacao final cobre o contrato acumulado — se ela
cobrisse so a ultima tela, o browser teria preenchido quatro telas e a intent
valido uma.

`LoopPhase` (prepare/inspect/resolve/fill/advance/submit/observe) e interno:
nenhum `ApplicationState` novo, porque o enum representa decisao de dominio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol

from .application import TRANSITIONS, ApplicationService, evaluate_safety_gate
from .ats import ATSAdapter
from .coordinator import SubmissionCoordinator, SubmissionResult
from .journey import ApplicationJourney, is_answered
from .models import (
    Application,
    ApplicationContext,
    ApplicationForm,
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
from .resolver import QuestionResolver
from .submission_policy import Channel

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
        # Uma submissao FOI ENTREGUE e o provedor nao confirmou. Reabrir o
        # browser sozinho refaria o preenchimento e tentaria a intent de novo —
        # e a intent recusa (`requires READY_TO_APPLY`), o que virava excecao
        # crua em vez de resultado. Quem reabre e uma pessoa, por
        # `application retry-submit`. Achado no cenario 2 da suite de integracao.
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
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
    #: Curriculo dinamico (dict serializado): contexto autorizado para geracao.
    resume: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplicationLoopResult:
    """Contrato final do loop: o mesmo em qualquer caminho de saida, sem PII.

    Em multi-step, `cycles` e o numero de telas inspecionadas,
    `steps_completed` os avancos concluidos, e `questions_answered` /
    `unanswered_required` vem do contrato ACUMULADO — nunca da ultima tela.
    """

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
    #: Escritas usadas para SUBIR o curriculo ao storage do board. Sao um
    #: orcamento separado do de submissao — misturar os dois esconderia qual
    #: permissao foi consumida.
    upload_writes_used: int = 0
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
    #: Gerador de resposta discursiva (JSA-QA-001E). Ausente = pergunta aberta
    #: cai em NEEDS_HUMAN, e nao em texto inventado.
    answer_provider: Any | None = None
    #: Ausente = politica declarada pelo provider (`LiveNetworkPolicy.for_submission`).
    policy_for: PolicyFactory | None = None
    #: Permissoes de escrita para a SUBIDA do curriculo, montadas por quem
    #: conhece o provider (o pipeline). O loop so as arma durante o
    #: preenchimento e SOMENTE quando o envio foi autorizado: com `--submit`
    #: ausente nada e armado e o dry-run continua sem nenhuma escrita.
    upload_permits: Callable[[Job, ATSAdapter, str], list[Any]] | None = None
    #: Integracao com o subsistema de resolucao (opt-in). Quando presente, ela
    #: substitui o gate: o loop passa a chamar o ORQUESTRADOR (que coordena
    #: engine, estrategia, executor e validador) em vez da politica inline.
    challenge_integration: Any | None = None
    #: Gate de challenge ANTES da escrita (opt-in). `None` = comportamento
    #: legado, byte a byte: o loop so descobre desafio quando o proprio submit
    #: falha. Ligado por `ENABLE_CHALLENGE_RESOLUTION=true` no runtime de
    #: producao. Ele apenas observa — nunca interage com o desafio.
    challenge_gate: Any | None = None
    #: Orcamento (segundos) para uma PESSOA resolver o desafio na janela visivel
    #: enquanto o gate reobserva. Zero = uma unica leitura, e desafio presente
    #: ja bloqueia.
    challenge_wait_seconds: float = 0.0
    #: Relay do operador. Existe junto com o gate (mesma flag): e por `offer()`
    #: que uma superficie de operador — hoje um teste, amanha um CLI/HTTP —
    #: entrega comandos para a thread que possui a pagina. O relay nunca da
    #: acesso ao controle de envio: `drain()` recusa clique sobre submit e
    #: teclas que submetem, e o submit fica desabilitado durante a janela.
    live_view_relay: Any | None = None
    #: Endereco que RECEBE o POST da candidatura. Nao e o mesmo que a URL do
    #: formulario: no Workable o formulario vive em
    #: `/apply.workable.com/<account>/j/<shortcode>/apply` e a candidatura sobe
    #: para `/apply.workable.com/api/v1/accounts/<account>/jobs/<shortcode>/applications`.
    #: Usar a URL do formulario como destino (o comportamento anterior) mandava a
    #: intent, a policy e a observacao para o endereco errado — o provider nunca
    #: seria certificado por mais correto que o preenchimento estivesse.
    submission_destination: Callable[[Job, ATSAdapter, ApplicationForm], str] | None = None
    #: Canal de API (opt-in). Quando presente E quando a politica de dominio
    #: autoriza o destino (credencial DECLARADA para aquele dominio), o loop
    #: entrega a escrita a ele em vez do `SubmissionCoordinator` do browser. O
    #: miolo (snapshot, intent, tentativa, estado) e o mesmo; o que muda e por
    #: onde a requisicao sai — e o guard, que e proprio do canal.
    submission_router: Any | None = None
    #: Campo do curriculo no corpo do POST de API. Nome POR PROVIDER e nao
    #: medido em nenhum: exigido de quem chama, nunca inventado aqui.
    api_resume_field: str = ""
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
        # Uma janela do operador aberta por um processo que morreu e fechada AQUI,
        # antes de qualquer trabalho novo: a evidencia do abandono fica gravada e
        # a execucao comeca sem herdar uma janela fantasma.
        self._reconcile_handoffs(application)
        material = self.runtime.prepare(job)
        application = self._record_materials(service, application, material)
        adapter = self.runtime.adapter_for(job)
        # Um acumulador por execucao: e ele que responde pelo TODO.
        journey = ApplicationJourney(provider=str(getattr(adapter, "provider", "")), job_id=job.id)
        form_url = self.runtime.form_url(job, adapter)
        session: Any = None
        try:
            session = self.runtime.open_session(job, adapter)
            phases.append(LoopPhase.INSPECT.value)
            # O curriculo sobe para o storage do board por POST DURANTE o
            # preenchimento. Sem essa permissao o arquivo nunca chega ao board e
            # o campo continua vazio no DOM: o proprio site recusa o envio com
            # "Resume/CV is required" mesmo com o anexo no modelo. So acontece
            # com envio autorizado — sem `--submit` o dry-run segue sem escrita.
            upload_armed = self._arm_uploads(session, job, adapter, application.id) if submit else False
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
                resolver=self._resolver(job, material),
                journey=journey,
            )
            live = orchestrator.run(session, context, form_url)
            upload_writes_used = self._upload_writes_used(session) if upload_armed else 0
            if not journey.steps and live.form is not None:
                # Blindagem: se algum caminho devolver formulario sem registrar a
                # etapa, o contrato passa a valer mesmo assim. Sem isto os totais
                # do resultado sairiam zerados em silencio.
                journey.add_step(
                    index=1,
                    form=live.form,
                    form_fingerprint=live.form_fingerprint or "",
                    url=live.url,
                    decisions=dict(context.validation.get("question_resolution", {})),
                )
            phases.extend([LoopPhase.RESOLVE.value, LoopPhase.FILL.value])
            if upload_armed:
                session.disarm_authorized_write()
            if live.advanced_steps:
                phases.append(LoopPhase.ADVANCE.value)

            # A decisao de dominio vem do Safety Gate que o orquestrador rodou; o
            # contrato acumulado fica registrado junto, ANTES de qualquer
            # ramificacao — o caminho de falha tambem precisa de rastro.
            application = self._record_decision(service, application, live, context, journey)

            if live.status != "FILLED_REVIEW_REQUIRED":
                # O orquestrador ja decidiu: preenchimento incompleto, formulario
                # nao suportado, loop de etapas, contradicao de contrato ou erro
                # de browser. Nao se forca a resolucao "de qualquer jeito".
                # O material FOI preparado: o hash dele continua no resultado,
                # senao a saida antecipada esconderia o que ja existe.
                return self._without_submit(
                    application,
                    job,
                    live,
                    journey,
                    phases,
                    resume_sha256=material.resume_sha256,
                    answers_fingerprint=journey.answers_fingerprint(),
                    upload_writes_used=upload_writes_used,
                )

            handled = self._handle_challenge(session, application)
            if handled is not None:
                # Desafio presente e nao resolvido dentro do orcamento: nao ha
                # escrita, nao ha intent armada, e o estado e retomavel.
                return self._challenge_blocked(
                    service,
                    application,
                    job,
                    live,
                    journey,
                    phases,
                    gate=handled,
                    resume_sha256=material.resume_sha256,
                    answers_fingerprint=journey.answers_fingerprint(),
                )

            form = live.form
            # `form_fingerprint` = superficie FINAL onde o Submit acontece.
            # `answers_fingerprint` = TODAS as respostas, de TODAS as etapas.
            form_fingerprint = live.form_fingerprint
            answers_fingerprint = journey.answers_fingerprint()
            application = self._record_review(service, application, live, journey)
            if not submit:
                return self._result(
                    application,
                    job,
                    status="READY_TO_SUBMIT",
                    live=live,
                    journey=journey,
                    phases=phases,
                    resume_sha256=material.resume_sha256,
                    answers_fingerprint=answers_fingerprint,
                    terminal=False,
                    requires_action="submit",
                    reason="preenchimento concluido; envio nao autorizado nesta execucao",
                    upload_writes_used=upload_writes_used,
                )

            phases.append(LoopPhase.SUBMIT.value)
            destination = self._submission_destination(job, adapter, form, form_url)
            submission = self._submit_authorized(
                application=application,
                job=job,
                form=form,
                journey=journey,
                session=session,
                provider=adapter.provider,
                destination=destination,
                material=material,
                form_fingerprint=form_fingerprint,
                answers_fingerprint=answers_fingerprint,
            )
            phases.append(LoopPhase.OBSERVE.value)
            return self._from_submission(
                application,
                job,
                live,
                journey,
                submission,
                material,
                form_fingerprint,
                answers_fingerprint,
                phases,
                upload_writes_used=upload_writes_used,
            )
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    def _submit_authorized(
        self,
        *,
        application: Application,
        job: Job,
        form: ApplicationForm,
        journey: ApplicationJourney,
        session: Any,
        provider: str,
        destination: str,
        material: PreparedMaterial,
        form_fingerprint: str,
        answers_fingerprint: str,
    ) -> SubmissionResult:
        """A escrita autorizada, pelo canal que a politica de dominio decidir.

        O roteamento NAO e heuristica: `SubmissionRouter.channel_for` so devolve
        `API` quando existe credencial declarada para o dominio do destino; em
        qualquer outro caso (inclusive credencial ausente) o canal e o browser, e
        a degradacao e a mesma que `DomainPolicy` ja testa. Assim um erro de
        configuracao nao vira uma tentativa de escrita sem credencial.
        """
        router = self.runtime.submission_router
        if router is not None and router.channel_for(destination) is Channel.API:
            # Nao ha `except` aqui de proposito: um erro de roteamento e um bug de
            # configuracao, e cair em silencio no canal de browser trocaria o
            # canal de uma escrita autorizada sem que ninguem soubesse.
            return router.submit(
                application=application,
                job=job,
                form=form,
                journey=journey,
                provider=provider,
                destination=destination,
                resume_path=material.resume_path,
                resume_sha256=material.resume_sha256,
                resume_field=self.runtime.api_resume_field,
                form_fingerprint=form_fingerprint,
                answers_fingerprint=answers_fingerprint,
                allow_insecure_destination=self.runtime.allow_insecure_destination,
            )
        return self.coordinator.submit(
            application=application,
            job=job,
            form=form,
            session=session,
            provider=provider,
            destination=destination,
            resume_sha256=material.resume_sha256,
            form_fingerprint=form_fingerprint,
            answers_fingerprint=answers_fingerprint,
            allow_insecure_destination=self.runtime.allow_insecure_destination,
            policy_factory=self.runtime.policy_for,
            journey=journey,
        )

    # -- upload ----------------------------------------------------------------

    def _arm_uploads(self, session: Any, job: Job, adapter: ATSAdapter, application_id: str) -> bool:
        """Arma o orcamento de upload antes do preenchimento. Devolve se armou."""
        builder = self.runtime.upload_permits
        if builder is None:
            return False
        permits = list(builder(job, adapter, application_id) or [])
        if not permits:
            return False
        session.arm_writes(permits)
        return True

    @staticmethod
    def _upload_writes_used(session: Any) -> int:
        guard = getattr(session, "network_guard", None)
        try:
            return int(getattr(guard, "authorized_writes_used", 0) or 0)
        except (TypeError, ValueError):  # pragma: no cover - defensivo
            return 0

    # -- challenge antes da escrita --------------------------------------------

    def _reconcile_handoffs(self, application: Application) -> None:
        """Fecha janelas do operador que ficaram abertas por restart."""
        integration = self.runtime.challenge_integration
        if integration is None or getattr(integration, "database", None) is None:
            return
        try:
            from .challenge_integration import reconcile_abandoned_handoffs

            reconcile_abandoned_handoffs(self.database, application.id)
        except Exception:  # pragma: no cover - reconciliar nunca derruba o loop
            pass

    def _handle_challenge(self, session: Any, application: Application) -> Any | None:
        """Trata o desafio antes da escrita. Devolve o bloqueio, ou `None`.

        Com `challenge_integration` configurada, quem decide e o ORQUESTRADOR
        (observar -> resolver -> validar, com limites e journal). Sem ela, cai no
        gate, que e a politica inline — mesmo comportamento observavel, um caminho
        a menos para manter quando a integracao estiver ligada em producao.
        """
        integration = self.runtime.challenge_integration
        page = getattr(session, "page", None)
        if integration is not None and page is not None:
            result = integration.handle(page, application.id)
            if not result.blocks:
                return None
            return result
        gate = self._evaluate_challenge_gate(session, application)
        if gate is not None and gate.blocking:
            return gate
        return None

    def _evaluate_challenge_gate(self, session: Any, application: Application):
        """Observa o estado anti-bot antes de qualquer autorizacao de escrita.

        Sem gate configurado, devolve `None` e nada muda. Com gate, a unica
        acao no browser e a observacao do `challenge-guard`.
        """
        gate = self.runtime.challenge_gate
        page = getattr(session, "page", None)
        if gate is None or page is None:
            return None
        try:
            return gate.evaluate(page, wait_seconds=self.runtime.challenge_wait_seconds)
        except Exception as exc:  # gate quebrado nunca libera escrita
            from .challenge_gate import ChallengeGateResult

            return ChallengeGateResult(
                blocking=True,
                resolved=False,
                decision=f"gate_failed:{type(exc).__name__}",
                provider="",
                rounds=0,
                waited_seconds=0.0,
            )

    def _challenge_blocked(
        self,
        service: ApplicationService,
        application: Application,
        job: Job,
        live: LiveApplicationResult,
        journey: ApplicationJourney,
        phases: list[str],
        *,
        gate: Any,
        resume_sha256: str,
        answers_fingerprint: str,
    ) -> ApplicationLoopResult:
        """Persiste `NEEDS_CAPTCHA` e para, com zero escritas.

        `NEEDS_CAPTCHA` (e nao `NEEDS_HUMAN_CAPTCHA`): nada saiu do browser, e
        este estado e retomavel por desenho (`NEEDS_CAPTCHA -> PREPARING`). O
        vocabulario distingue "a etapa foi interrompida por um desafio" de "o
        provedor recusou uma candidatura entregue".
        """
        state = self._state(application.id)
        payload = gate.as_journal() if hasattr(gate, "as_journal") else {}
        decision = getattr(gate, "decision", "") or payload.get("final_status", "")
        if ApplicationState.NEEDS_CAPTCHA in TRANSITIONS[state]:
            application = service.transition(
                application.id,
                ApplicationState.NEEDS_CAPTCHA,
                "challenge_detected_before_submit",
                payload,
            )
        return self._result(
            application,
            job,
            status="NEEDS_CAPTCHA",
            live=live,
            journey=journey,
            phases=phases,
            resume_sha256=resume_sha256,
            answers_fingerprint=answers_fingerprint,
            terminal=False,
            requires_action=ApplicationState.NEEDS_CAPTCHA.value,
            reason=f"challenge_before_submit:{decision}",
        )

    # -- destino da submissao --------------------------------------------------

    def _submission_destination(
        self, job: Job, adapter: ATSAdapter, form: ApplicationForm, form_url: str
    ) -> str:
        """Endereco do POST: declarado pelo provider, nunca presumido da URL.

        Sem o hook, o destino e a propria URL do formulario — que e o caso do
        Lever e do Greenhouse moderno, mas nao de todo ATS.
        """
        builder = self.runtime.submission_destination
        if builder is None:
            return form_url
        return str(builder(job, adapter, form) or form_url)

    # -- resolucao -------------------------------------------------------------

    def _resolver(self, job: Job, material: PreparedMaterial) -> QuestionResolver:
        """Um resolvedor por execucao: ele conhece a vaga e o material corrente."""
        return QuestionResolver(
            knowledge=self.runtime.answers,
            profile=self.runtime.profile,
            preferences=self.runtime.preferences,
            job=job,
            resume=material.resume,
            provider=self.runtime.answer_provider,
        )

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
        journey: ApplicationJourney,
        submission: SubmissionResult,
        material: PreparedMaterial,
        form_fingerprint: str,
        answers_fingerprint: str,
        phases: list[str],
        upload_writes_used: int = 0,
    ) -> ApplicationLoopResult:
        state = self._state(application.id)
        terminal = state in TERMINAL_STATES
        return self._result(
            application,
            job,
            status=submission.status,
            live=live,
            journey=journey,
            phases=phases,
            resume_sha256=material.resume_sha256,
            answers_fingerprint=answers_fingerprint,
            terminal=terminal,
            requires_action="" if terminal else state.value,
            reason=submission.error,
            submission_attempted=True,
            submission_writes=submission.writes,
            upload_writes_used=upload_writes_used,
            form_fingerprint=form_fingerprint,
        )

    def _without_submit(
        self,
        application: Application,
        job: Job,
        live: LiveApplicationResult,
        journey: ApplicationJourney,
        phases: list[str],
        *,
        resume_sha256: str = "",
        answers_fingerprint: str = "",
        upload_writes_used: int = 0,
    ) -> ApplicationLoopResult:
        state = self._state(application.id)
        recoverable = state in RECOVERABLE_STATES
        status = live.status
        requires_action = state.value if recoverable else ""
        if status in {"LOOP_DETECTED", "CONTRACT_CONFLICT"}:
            # Repetir o mesmo formulario, ou conviver com respostas contraditorias,
            # nao e para tentar de novo: e defeito de fluxo e exige uma pessoa.
            recoverable = False
            requires_action = status
        return self._result(
            application,
            job,
            status=status,
            live=live,
            journey=journey,
            phases=phases,
            resume_sha256=resume_sha256,
            answers_fingerprint=answers_fingerprint,
            terminal=not recoverable,
            requires_action=requires_action,
            reason=live.error or status,
            upload_writes_used=upload_writes_used,
        )

    def _result(
        self,
        application: Application,
        job: Job,
        *,
        status: str,
        live: LiveApplicationResult,
        phases: list[str],
        journey: ApplicationJourney | None = None,
        resume_sha256: str = "",
        answers_fingerprint: str = "",
        terminal: bool,
        requires_action: str = "",
        reason: str = "",
        submission_attempted: bool = False,
        submission_writes: int = 0,
        upload_writes_used: int = 0,
        form_fingerprint: str = "",
    ) -> ApplicationLoopResult:
        # Os totais vem do CONTRATO acumulado, nao da ultima tela: numa
        # candidatura de quatro etapas, "respondi 13 perguntas" tem de ser 13.
        return ApplicationLoopResult(
            application_id=application.id,
            job_id=job.id,
            state=self._state(application.id),
            status=status,
            provider=str(getattr(journey, "provider", "") or live.provider or ""),
            cycles=journey.cycles if journey is not None else len(live.cycles),
            steps_completed=journey.steps_completed if journey is not None else live.advanced_steps,
            questions_answered=journey.answered_count() if journey is not None else 0,
            unanswered_required=journey.pending_required() if journey is not None else (),
            resume_sha256=resume_sha256,
            form_fingerprint=form_fingerprint or live.form_fingerprint,
            answers_fingerprint=answers_fingerprint
            or (journey.answers_fingerprint() if journey is not None else ""),
            submission_attempted=submission_attempted,
            submission_writes=submission_writes,
            upload_writes_used=upload_writes_used,
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
        journey: ApplicationJourney,
    ) -> Application:
        """Persiste a decisao do Safety Gate, o formulario e o contrato acumulado."""
        readiness = live.readiness or evaluate_safety_gate(context)
        # O contexto do orquestrador volta para a Application: e nele que ficam a
        # decisao por campo e as respostas resolvidas. O `resume_sha256` e
        # reaplicado porque ele vem do material, nao da inspecao.
        merged = to_dict(context)
        merged["resume_sha256"] = str(application.context.get("resume_sha256", ""))
        merged["journey"] = journey.to_dict()
        application.context = merged
        application.context["readiness"] = to_dict(readiness)
        self.database.save_application(application)
        if live.form is not None:
            self.database.save_application_form(application.id, live.form)
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
        journey: ApplicationJourney | None = None,
    ) -> Application:
        """Registra que o preenchimento chegou a superficie de revisao.

        Persiste o formulario ACUMULADO (`journey.as_form()`), o mesmo material
        que o snapshot de review aprova. Gravar apenas a ultima tela deixava o
        banco com um formulario diferente do snapshot, e o handoff humano
        recusava por integridade ("approved answers changed after the review
        snapshot") numa candidatura de mais de uma tela — exatamente o caminho
        que fecha a funcao central do produto.
        """
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
        if journey is not None and journey.steps:
            self.database.save_application_form(application.id, journey.as_form())
        return application

    def _state(self, application_id: str) -> ApplicationState:
        application = self.database.get_application(application_id)
        if application is None:  # pragma: no cover - defensivo
            raise LoopError(f"application not found: {application_id}")
        return application.state


__all__ = [
    "ApplicationJourney",
    "ApplicationLoop",
    "ApplicationLoopResult",
    "LoopError",
    "LoopPhase",
    "LoopRuntime",
    "NO_RESEND_STATES",
    "PreparedMaterial",
    "RECOVERABLE_STATES",
    "TERMINAL_STATES",
    "is_answered",
]
