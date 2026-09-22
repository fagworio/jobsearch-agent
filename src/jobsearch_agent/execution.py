"""Browser Dry Run execution contracts.

This module produces and executes only fill/upload actions.  Submission is not
represented in the model, which makes accidental application submission
impossible at this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .forms import validate_application_form
from .application import evaluate_safety_gate
from .models import ApplicationContext, ApplicationField, ValidationResult, now_iso


class ExecutionPlanError(ValueError):
    pass


@dataclass
class ExecutionAction:
    action_type: str
    field_key: str
    value: Any = ""
    attachment_path: str = ""
    step: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    application_id: str
    provider: str
    actions: list[ExecutionAction] = field(default_factory=list)
    final_action: str = "STOP_BEFORE_SUBMIT"
    version: str = "1"
    created_at: str = field(default_factory=now_iso)


def _field_value(field: ApplicationField) -> Any:
    if field.value not in (None, ""):
        return field.value
    return field.answer.answer if field.answer else ""


def validate_execution_plan(plan: ExecutionPlan) -> ValidationResult:
    errors: list[str] = []
    if plan.final_action != "STOP_BEFORE_SUBMIT":
        errors.append("execution plan must stop before submit")
    for action in plan.actions:
        if action.action_type.casefold() in {"submit", "send", "apply"}:
            errors.append("submission action is forbidden in Browser Dry Run")
        if action.action_type not in {"fill", "upload"}:
            errors.append(f"unsupported execution action: {action.action_type}")
    return ValidationResult(not errors, "OK" if not errors else "INVALID_EXECUTION_PLAN", errors)


def build_execution_plan(context: ApplicationContext) -> ExecutionPlan:
    if context.form is None:
        raise ExecutionPlanError("cannot build execution plan before form inspection")
    readiness = evaluate_safety_gate(context)
    if not readiness.ready_to_apply:
        raise ExecutionPlanError("cannot build execution plan before Safety Gate approval: " + "; ".join(readiness.blockers or [readiness.decision.value]))
    form_validation = validate_application_form(context.form)
    if not form_validation.valid:
        raise ExecutionPlanError("cannot build execution plan for invalid form: " + "; ".join(form_validation.errors))
    actions: list[ExecutionAction] = []
    for field in context.form.fields:
        value = _field_value(field)
        if field.field_type == "file":
            if field.attachment_path:
                actions.append(ExecutionAction("upload", field.key, attachment_path=field.attachment_path, step=field.step, metadata={"semantic_type": field.semantic_type}))
        elif value not in (None, "", []):
            actions.append(ExecutionAction("fill", field.key, value=value, step=field.step, metadata={"semantic_type": field.semantic_type}))
    plan = ExecutionPlan(context.application_id, context.form.provider, actions)
    result = validate_execution_plan(plan)
    if not result.valid:
        raise ExecutionPlanError("invalid execution plan: " + "; ".join(result.errors))
    return plan
