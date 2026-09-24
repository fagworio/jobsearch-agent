"""Bounded, provider-neutral orchestration for Browser Dry Run cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Callable

from .application import evaluate_safety_gate
from .ats import ATSAdapter, adapter_for
from .browser import BrowserExecutionResult, DOMStabilityGuard, GuardedBrowserSession, PlaywrightFormFiller
from .execution import ExecutionPlan, build_execution_plan
from .inspector import FormBindings, InspectionError, compute_form_fingerprint
from .models import ApplicationAnswer, ApplicationContext, ApplicationForm, ApplicationReadiness, ApplicationState, CareerProfile, CandidatePreferences
from .qa import AnswerKnowledgeBase, question_key


@dataclass
class DryRunCycle:
    cycle: int
    form_fingerprint: str
    readiness: ApplicationState
    result: str = ""
    executed_actions: int = 0


@dataclass
class DryRunApplicationResult:
    status: str
    cycles: list[DryRunCycle] = field(default_factory=list)
    readiness: ApplicationReadiness | None = None
    execution: BrowserExecutionResult | None = None
    plan: ExecutionPlan | None = None
    error: str = ""


class DryRunApplicationOrchestrator:
    """Inspect, resolve, validate, plan and execute with a bounded retry loop."""

    def __init__(
        self,
        adapter: ATSAdapter,
        profile: CareerProfile,
        preferences: CandidatePreferences,
        answers: AnswerKnowledgeBase,
        filler: PlaywrightFormFiller | None = None,
        max_cycles: int = 5,
        resolver: Any | None = None,
    ):
        if max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        self.adapter = adapter
        self.profile = profile
        self.preferences = preferences
        self.answers = answers
        #: Resolvedor unificado (JSA-QA-001). Ausente = comportamento anterior,
        #: com o `AnswerKnowledgeBase` como unico resolvedor.
        self.resolver: Any = resolver
        self.filler = filler or PlaywrightFormFiller()
        self.max_cycles = max_cycles

    def run(
        self,
        session: GuardedBrowserSession,
        context: ApplicationContext,
        audit_dir: str | Path | None = None,
    ) -> DryRunApplicationResult:
        if self.profile.demo:
            return DryRunApplicationResult("DEMO_PROFILE_BLOCKED", error="demo profile cannot be used for public dry-run fill")
        if not getattr(session, "page", None):
            return DryRunApplicationResult("INVALID_SESSION", error="browser session has no page")
        page = session.page
        seen: set[str] = set()
        history: list[DryRunCycle] = []
        previous_form = context.form

        for cycle in range(1, self.max_cycles + 1):
            inspected = self.adapter.inspect(page.content(), str(getattr(page, "url", "")), form_id=previous_form.form_id if previous_form else "application")
            self._carry_artifacts(previous_form, inspected.form)
            self._carry_approved_answers(previous_form, inspected.form, list(context.answers))
            context.form = inspected.form
            self._resolve_fields(context)
            fingerprint = compute_form_fingerprint(inspected.form, inspected.bindings)
            if fingerprint in seen:
                return DryRunApplicationResult("LOOP_DETECTED", history, error=f"form fingerprint repeated at cycle {cycle}")
            seen.add(fingerprint)

            readiness = evaluate_safety_gate(context)
            entry = DryRunCycle(cycle, fingerprint, readiness.decision)
            history.append(entry)
            if not readiness.ready_to_apply:
                entry.result = readiness.decision.value
                return DryRunApplicationResult(readiness.decision.value, history, readiness, error="; ".join(readiness.blockers))

            plan = build_execution_plan(context, inspected.bindings)
            cycle_audit = Path(audit_dir) / f"cycle-{cycle}" if audit_dir else None
            execution = self.filler.fill(session, context, plan, inspected.bindings, audit_dir=cycle_audit)
            entry.result = execution.status
            entry.executed_actions = len(execution.operations)
            if execution.status == "FORM_CHANGED":
                previous_form = context.form
                continue
            return DryRunApplicationResult(execution.status, history, readiness, execution, plan)

        return DryRunApplicationResult("MAX_CYCLES_EXCEEDED", history, error=f"maximum cycles exceeded: {self.max_cycles}")

    def run_with_session_factory(
        self,
        session_factory: Callable[[], GuardedBrowserSession],
        context: ApplicationContext,
        audit_dir: str | Path | None = None,
    ) -> DryRunApplicationResult:
        """Enforce profile policy before creating a public browser session."""
        if self.profile.demo:
            return DryRunApplicationResult("DEMO_PROFILE_BLOCKED", error="demo profile cannot be used for public dry-run fill")
        session = session_factory()
        return self.run(session, context, audit_dir)

    def _resolve_fields(self, context: ApplicationContext) -> None:
        """Resolve cada campo. Com `QuestionResolver`, a decisao e dele.

        O orquestrador nao sabe se a resposta veio de resposta aprovada, do
        perfil, das preferencias ou de geracao grounded: ele so sabe que houve
        RESOLVED ou NEEDS_HUMAN. A decisao fica registrada no contexto para
        auditoria, sem o texto da resposta.
        """
        resolved: list[ApplicationAnswer] = []
        decisions: dict[str, dict[str, object]] = {}
        if context.form is None:
            return
        for field in context.form.fields:
            answer = field.answer if field.answer and field.answer.approved and field.answer.answer else None
            if answer is None and self.resolver is not None:
                resolution = self.resolver.resolve_field(field)
                decisions[field.key] = {
                    "status": resolution.status,
                    "source": resolution.source,
                    "reason": resolution.reason,
                    "supported_by": list(resolution.supported_by),
                }
                answer = resolution.to_answer(field)
            elif answer is None:
                answer = self.answers.resolve_field(field, self.profile, self.preferences)
            field.answer = answer
            if answer:
                resolved.append(answer)
        context.answers = resolved
        if decisions:
            context.validation["question_resolution"] = decisions

    @classmethod
    def _carry_approved_answers(cls, previous_form: Any, current_form: Any, approved_answers: list[ApplicationAnswer]) -> None:
        """Carry only answers that remain attributable to the same field contract."""
        if current_form is None:
            return
        previous_fields = list(getattr(previous_form, "fields", []) or [])
        field_candidates = {
            field.key: field.answer
            for field in previous_fields
            if field.answer and field.answer.approved and field.answer.answer
        }
        for field in current_form.fields:
            if field.answer and field.answer.approved and field.answer.answer:
                continue
            candidate = field_candidates.get(field.key)
            if candidate is not None and cls._answer_matches_field(candidate, field):
                if cls._option_is_still_valid(candidate, field):
                    field.answer = candidate
                    continue
            candidates = [answer for answer in approved_answers if cls._answer_matches_field(answer, field)]
            if len(candidates) == 1 and cls._option_is_still_valid(candidates[0], field):
                field.answer = candidates[0]

    @staticmethod
    def _normalize_question(value: str) -> str:
        return re.sub(r"\s+", " ", str(value).casefold()).strip()

    @classmethod
    def _answer_matches_field(cls, answer: ApplicationAnswer, field: Any) -> bool:
        if not answer.approved or not answer.answer:
            return False
        if answer.field_key and answer.field_key != field.key:
            return False
        if answer.semantic_type != field.semantic_type:
            return False
        question_matches = (
            cls._normalize_question(answer.question) == cls._normalize_question(field.label)
            or answer.question_key == question_key(field.label)
        )
        return question_matches

    @staticmethod
    def _option_is_still_valid(answer: ApplicationAnswer, field: Any) -> bool:
        if not field.options:
            return True
        normalized_answer = str(answer.answer).casefold().strip()
        return any(normalized_answer == str(option).casefold().strip() for option in field.options)

    @staticmethod
    def _carry_artifacts(previous_form: Any, current_form: Any) -> None:
        if previous_form is None:
            return
        if not current_form.artifact_root:
            current_form.artifact_root = previous_form.artifact_root
        old_by_semantic = {field.semantic_type: field for field in previous_form.fields if field.attachment_path}
        for field in current_form.fields:
            previous = next((item for item in previous_form.fields if item.key == field.key), None)
            if previous is None:
                previous = old_by_semantic.get(field.semantic_type)
            if previous and previous.attachment_path:
                field.attachment_path = previous.attachment_path


#: Button labels that only advance a multi-step form. A control must be an
#: explicit ``type="button"`` to qualify, so a native submit can never match.
_ADVANCE_LABELS = frozenset(
    {
        "next",
        "next step",
        "continue",
        "continue application",
        "save and continue",
        "proximo",
        "próximo",
        "continuar",
        "avancar",
        "avançar",
    }
)


@dataclass
class LiveApplicationResult:
    status: str
    provider: str = ""
    url: str = ""
    cycles: list[DryRunCycle] = field(default_factory=list)
    readiness: ApplicationReadiness | None = None
    execution: BrowserExecutionResult | None = None
    plan: ExecutionPlan | None = None
    form: ApplicationForm | None = None
    bindings: FormBindings | None = None
    form_fingerprint: str = ""
    advanced_steps: int = 0
    #: field_key -> caminho do artefato, acumulado entre ciclos. O widget de
    #: upload do Greenhouse remove o input depois do envio, entao a ultima
    #: inspecao pode nao conter mais o campo; o anexo continua valido.
    attachments: dict[str, str] = field(default_factory=dict)
    error: str = ""


def _click_safe_apply_button(page: Any) -> bool:
    """Reveal the application form through a non-submit Apply control only."""
    button = page.locator('button[aria-label="Apply"]')
    if button.count() != 1:
        return False
    if (button.get_attribute("type") or "submit").casefold() != "button":
        return False
    button.click()
    return True


class LiveApplicationOrchestrator(DryRunApplicationOrchestrator):
    """Drive a real page through fill/upload and stop before any submission.

    It reuses the bounded inspect -> resolve -> Safety Gate -> plan -> fill
    cycle of the dry-run orchestrator, but against a live guarded session: the
    browser performs the navigation, the field filling and the resume upload,
    while every network write stays blocked by ``NetworkWriteGuard``. Submission
    is never reachable from this layer.
    """

    def __init__(
        self,
        adapter: ATSAdapter,
        profile: CareerProfile,
        preferences: CandidatePreferences,
        answers: AnswerKnowledgeBase,
        filler: PlaywrightFormFiller | None = None,
        max_cycles: int = 5,
        allow_advance: bool = True,
        artifact_root: str = "",
        default_resume: str = "",
        resolver: Any | None = None,
    ):
        super().__init__(adapter, profile, preferences, answers, filler, max_cycles, resolver)
        self.allow_advance = allow_advance
        self.artifact_root = artifact_root
        self.default_resume = default_resume

    def run(
        self,
        session: GuardedBrowserSession,
        context: ApplicationContext,
        url: str,
        audit_dir: str | Path | None = None,
    ) -> LiveApplicationResult:
        if self.profile.demo:
            return LiveApplicationResult("DEMO_PROFILE_BLOCKED", url=url, error="demo profile cannot be used for public fill")
        if not getattr(session, "page", None):
            return LiveApplicationResult("INVALID_SESSION", url=url, error="browser session has no page")
        page = session.page
        try:
            session.open(url)
        except Exception as exc:
            return LiveApplicationResult("NAVIGATION_FAILED", url=url, error=str(exc))

        history: list[DryRunCycle] = []
        seen: set[str] = set()
        advanced_steps = 0
        attachments: dict[str, str] = {}
        previous_form = context.form
        try:
            for cycle in range(1, self.max_cycles + 1):
                self._settle(session)
                inspected, error = self._discover_form(session, page, previous_form, cycle)
                if inspected is None:
                    status = "UNSUPPORTED_PROVIDER" if cycle == 1 else "UNSUPPORTED_FORM"
                    return LiveApplicationResult(status, url=str(page.url), cycles=history, error=error)
                self._carry_artifacts(previous_form, inspected.form)
                self._carry_approved_answers(previous_form, inspected.form, list(context.answers))
                context.form = inspected.form
                self._apply_artifact_defaults(inspected.form)
                for candidate in inspected.form.fields:
                    if candidate.attachment_path:
                        attachments[candidate.key] = candidate.attachment_path
                self._resolve_fields(context)
                fingerprint = compute_form_fingerprint(inspected.form, inspected.bindings)
                if fingerprint in seen:
                    return LiveApplicationResult(
                        "LOOP_DETECTED",
                        provider=inspected.form.provider,
                        url=str(page.url),
                        cycles=history,
                        error=f"form fingerprint repeated at cycle {cycle}",
                    )
                seen.add(fingerprint)

                readiness = evaluate_safety_gate(context)
                entry = DryRunCycle(cycle, fingerprint, readiness.decision)
                history.append(entry)
                review_ready = readiness.decision == ApplicationState.READY_FOR_REVIEW and not readiness.blockers
                if not readiness.ready_to_apply and not review_ready:
                    entry.result = readiness.decision.value
                    return LiveApplicationResult(
                        readiness.decision.value,
                        provider=inspected.form.provider,
                        url=str(page.url),
                        cycles=history,
                        readiness=readiness,
                        form=inspected.form,
                        bindings=inspected.bindings,
                        form_fingerprint=fingerprint,
                        advanced_steps=advanced_steps,
                        error="; ".join(readiness.blockers) or readiness.decision.value,
                    )

                plan = build_execution_plan(context, inspected.bindings, allow_review=True)
                cycle_audit = Path(audit_dir) / f"cycle-{cycle}" if audit_dir else None
                execution = self.filler.fill(
                    session,
                    context,
                    plan,
                    inspected.bindings,
                    audit_dir=cycle_audit,
                    allow_review=True,
                )
                entry.result = execution.status
                entry.executed_actions = len(execution.operations)
                if execution.status == "FORM_CHANGED":
                    previous_form = context.form
                    continue
                if execution.status != "COMPLETED":
                    return LiveApplicationResult(
                        execution.status,
                        provider=inspected.form.provider,
                        url=str(page.url),
                        cycles=history,
                        readiness=readiness,
                        execution=execution,
                        plan=plan,
                        form=inspected.form,
                        bindings=inspected.bindings,
                        form_fingerprint=fingerprint,
                        advanced_steps=advanced_steps,
                        error=execution.reason,
                    )
                advance = self._advance(page, session) if self.allow_advance else "NO_STEP"
                if advance == "ADVANCE_BLOCKED":
                    return LiveApplicationResult(
                        "ADVANCE_BLOCKED_BY_NETWORK_POLICY",
                        provider=inspected.form.provider,
                        url=str(page.url),
                        cycles=history,
                        readiness=readiness,
                        execution=execution,
                        plan=plan,
                        form=inspected.form,
                        bindings=inspected.bindings,
                        form_fingerprint=fingerprint,
                        advanced_steps=advanced_steps,
                        error="a step control attempted a network write; the guarded session blocked it",
                    )
                if advance == "ADVANCED":
                    advanced_steps += 1
                    previous_form = context.form
                    continue
                return LiveApplicationResult(
                    "FILLED_REVIEW_REQUIRED",
                    provider=inspected.form.provider,
                    url=str(page.url),
                    cycles=history,
                    readiness=readiness,
                    execution=execution,
                    plan=plan,
                    form=inspected.form,
                    bindings=inspected.bindings,
                    form_fingerprint=fingerprint,
                    advanced_steps=advanced_steps,
                    attachments=attachments,
                )
        except Exception as exc:  # browser or runtime failure: never fail open
            return LiveApplicationResult("ERROR", url=str(getattr(page, "url", url)), cycles=history, error=str(exc))
        return LiveApplicationResult(
            "MAX_CYCLES_EXCEEDED",
            url=str(getattr(page, "url", url)),
            cycles=history,
            advanced_steps=advanced_steps,
            error=f"maximum cycles exceeded: {self.max_cycles}",
        )

    @staticmethod
    def _settle(session: GuardedBrowserSession) -> None:
        guard = session.network_guard
        DOMStabilityGuard().wait(
            session.page,
            (lambda: guard.pending_read_count) if guard is not None else None,
        )

    def _apply_artifact_defaults(self, form: ApplicationForm) -> None:
        """Attach the generated resume only to an unset resume file control."""
        if self.artifact_root and not form.artifact_root:
            form.artifact_root = self.artifact_root
        if not self.default_resume:
            return
        if not Path(self.default_resume).is_file():
            return
        for field in form.fields:
            if field.field_type.casefold() == "file" and field.semantic_type == "resume" and not field.attachment_path:
                field.attachment_path = self.default_resume

    def _discover_form(
        self,
        session: GuardedBrowserSession,
        page: Any,
        previous_form: ApplicationForm | None,
        cycle: int,
    ) -> tuple[Any | None, str]:
        """Find the application form in the page or in a nested frame."""
        form_id = previous_form.form_id if previous_form else "application"
        try:
            return self._inspect_frames(page, form_id), ""
        except InspectionError as exc:
            if cycle == 1 and _click_safe_apply_button(page):
                self._settle(session)
                try:
                    return self._inspect_frames(page, form_id), ""
                except InspectionError as retry_exc:
                    return None, str(retry_exc)
            return None, str(exc)

    def _inspect_frames(self, page: Any, form_id: str) -> Any:
        last_error = "no supported ATS application form matched the page"
        for frame in list(getattr(page, "frames", []) or [page]):
            frame_url = str(getattr(frame, "url", "") or "")
            try:
                html = frame.content()
            except Exception:
                continue
            detected = self.adapter if self.adapter.matches(frame_url, html) else adapter_for(frame_url, html)
            if detected is None:
                continue
            try:
                return detected.inspect(html, frame_url, form_id=form_id)
            except InspectionError as exc:
                last_error = str(exc)
                continue
        raise InspectionError(last_error)

    @staticmethod
    def _advance(page: Any, session: GuardedBrowserSession) -> str:
        """Click a single non-submit step control; never a submit control."""
        buttons = page.locator('button[type="button"]')
        candidates = []
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if not button.is_visible():
                    continue
                label = " ".join((button.inner_text() or "").split()).casefold()
            except Exception:
                continue
            if label in _ADVANCE_LABELS:
                candidates.append(button)
        if len(candidates) != 1:
            return "NO_STEP"
        guard = session.network_guard
        blocked_before = len(guard.blocked_writes) if guard is not None else 0
        candidates[0].click()
        LiveApplicationOrchestrator._settle(session)
        if guard is not None and len(guard.blocked_writes) > blocked_before:
            return "ADVANCE_BLOCKED"
        return "ADVANCED"
