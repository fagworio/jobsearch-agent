"""Bounded, provider-neutral orchestration for Browser Dry Run cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from .application import evaluate_safety_gate
from .ats import ATSAdapter
from .browser import BrowserExecutionResult, GuardedBrowserSession, PlaywrightFormFiller
from .execution import ExecutionPlan, build_execution_plan
from .inspector import compute_form_fingerprint
from .models import ApplicationAnswer, ApplicationContext, ApplicationReadiness, ApplicationState, CareerProfile, CandidatePreferences
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

    def _resolve_fields(self, context: ApplicationContext) -> None:
        resolved: list[ApplicationAnswer] = []
        if context.form is None:
            return
        for field in context.form.fields:
            answer = field.answer if field.answer and field.answer.approved and field.answer.answer else None
            if answer is None:
                answer = self.answers.resolve_field(field, self.profile, self.preferences)
            field.answer = answer
            if answer:
                resolved.append(answer)
        context.answers = resolved

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
