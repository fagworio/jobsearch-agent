"""Bounded, provider-neutral orchestration for Browser Dry Run cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .application import evaluate_safety_gate
from .ats import ATSAdapter
from .browser import BrowserExecutionResult, GuardedBrowserSession, PlaywrightFormFiller
from .execution import ExecutionPlan, build_execution_plan
from .inspector import compute_form_fingerprint
from .models import ApplicationAnswer, ApplicationContext, ApplicationReadiness, ApplicationState, CareerProfile, CandidatePreferences
from .qa import AnswerKnowledgeBase


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
    ):
        if max_cycles < 1:
            raise ValueError("max_cycles must be positive")
        self.adapter = adapter
        self.profile = profile
        self.preferences = preferences
        self.answers = answers
        self.filler = filler or PlaywrightFormFiller()
        self.max_cycles = max_cycles

    def run(
        self,
        session: GuardedBrowserSession,
        context: ApplicationContext,
        audit_dir: str | Path | None = None,
    ) -> DryRunApplicationResult:
        if not getattr(session, "page", None):
            return DryRunApplicationResult("INVALID_SESSION", error="browser session has no page")
        page = session.page
        seen: set[str] = set()
        history: list[DryRunCycle] = []
        previous_form = context.form

        for cycle in range(1, self.max_cycles + 1):
            inspected = self.adapter.inspect(page.content(), str(getattr(page, "url", "")), form_id=previous_form.form_id if previous_form else "application")
            self._carry_artifacts(previous_form, inspected.form)
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

    def _resolve_fields(self, context: ApplicationContext) -> None:
        resolved: list[ApplicationAnswer] = []
        if context.form is None:
            return
        for field in context.form.fields:
            answer = self.answers.resolve_field(field, self.profile, self.preferences)
            field.answer = answer
            if answer:
                resolved.append(answer)
        context.answers = resolved

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
