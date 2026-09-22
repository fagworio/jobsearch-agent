"""Browser Dry Run execution contracts.

This module produces and executes only fill/upload actions.  Submission is not
represented in the model, which makes accidental application submission
impossible at this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any

from .forms import effective_attachment_path, validate_application_form
from .application import evaluate_safety_gate
from .inspector import FormBindings, compute_form_fingerprint, validate_bindings_against_html, validate_form_bindings
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
    sha256: str = ""


@dataclass
class ExecutionPlan:
    application_id: str
    provider: str
    actions: list[ExecutionAction] = field(default_factory=list)
    final_action: str = "STOP_BEFORE_SUBMIT"
    version: str = "1"
    created_at: str = field(default_factory=now_iso)
    form_fingerprint: str = ""


def _field_value(field: ApplicationField) -> Any:
    if field.value not in (None, ""):
        return field.value
    return field.answer.answer if field.answer else ""


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_plan_against_context(context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings | None = None) -> ValidationResult:
    errors: list[str] = []
    if plan.application_id != context.application_id:
        errors.append("execution plan application does not match context")
    if context.form is None:
        errors.append("application form is missing")
        return ValidationResult(False, "INVALID_EXECUTION_CONTEXT", errors)
    if bindings is None:
        errors.append("form bindings are required at the execution boundary")
    else:
        binding_result = validate_form_bindings(context.form, bindings)
        errors.extend(binding_result.errors)
        if plan.form_fingerprint != compute_form_fingerprint(context.form, bindings):
            errors.append("execution plan form fingerprint is stale")
    if plan.provider != context.form.provider:
        errors.append("execution plan provider does not match form")
    fields = {field.key: field for field in context.form.fields}
    seen: set[str] = set()
    for action in plan.actions:
        if action.field_key in seen:
            errors.append(f"duplicate action for field: {action.field_key}")
        seen.add(action.field_key)
        field = fields.get(action.field_key)
        if field is None:
            errors.append(f"action references unknown field: {action.field_key}")
            continue
        field_type = field.field_type.casefold().strip()
        current_value = _field_value(field)
        if action.action_type == "upload":
            if field_type != "file":
                errors.append(f"upload action targets non-file field: {field.key}")
            if action.attachment_path != effective_attachment_path(field):
                errors.append(f"upload path does not match field artifact: {field.key}")
            else:
                try:
                    if not action.sha256 or action.sha256 != _sha256(action.attachment_path):
                        errors.append(f"upload hash does not match field artifact: {field.key}")
                except OSError:
                    errors.append(f"upload artifact is not readable: {field.key}")
        elif action.action_type == "fill":
            if field_type == "file":
                errors.append(f"fill action targets file field: {field.key}")
            if action.value != current_value:
                errors.append(f"fill value does not match current field value: {field.key}")
    return ValidationResult(not errors, "OK" if not errors else "INVALID_EXECUTION_CONTEXT", errors)


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


def build_execution_plan(context: ApplicationContext, bindings: FormBindings) -> ExecutionPlan:
    if context.form is None:
        raise ExecutionPlanError("cannot build execution plan before form inspection")
    binding_result = validate_form_bindings(context.form, bindings)
    if not binding_result.valid:
        raise ExecutionPlanError("cannot build execution plan with invalid bindings: " + "; ".join(binding_result.errors))
    readiness = evaluate_safety_gate(context)
    if not readiness.ready_to_apply:
        raise ExecutionPlanError("cannot build execution plan before Safety Gate approval: " + "; ".join(readiness.blockers or [readiness.decision.value]))
    form_validation = validate_application_form(context.form)
    if not form_validation.valid:
        raise ExecutionPlanError("cannot build execution plan for invalid form: " + "; ".join(form_validation.errors))
    actions: list[ExecutionAction] = []
    for field in context.form.fields:
        value = _field_value(field)
        field_type = field.field_type.casefold().strip()
        if field_type == "file":
            attachment_path = effective_attachment_path(field)
            if attachment_path:
                actions.append(ExecutionAction("upload", field.key, attachment_path=attachment_path, step=field.step, metadata={"semantic_type": field.semantic_type}, sha256=_sha256(attachment_path)))
        elif value not in (None, "", []):
            actions.append(ExecutionAction("fill", field.key, value=value, step=field.step, metadata={"semantic_type": field.semantic_type}))
    plan = ExecutionPlan(context.application_id, context.form.provider, actions, form_fingerprint=compute_form_fingerprint(context.form, bindings))
    result = validate_execution_plan(plan)
    if not result.valid:
        raise ExecutionPlanError("invalid execution plan: " + "; ".join(result.errors))
    context_result = _validate_plan_against_context(context, plan, bindings)
    if not context_result.valid:
        raise ExecutionPlanError("execution plan does not match context: " + "; ".join(context_result.errors))
    return plan


def validate_execution_context(context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings, current_html: str | None = None) -> ValidationResult:
    """Public defense-in-depth check used at the browser boundary."""
    readiness = evaluate_safety_gate(context)
    if not readiness.ready_to_apply:
        return ValidationResult(False, "SAFETY_GATE_BLOCKED", readiness.blockers or [readiness.decision.value])
    structural = validate_execution_plan(plan)
    if not structural.valid:
        return structural
    result = _validate_plan_against_context(context, plan, bindings)
    if not result.valid:
        return result
    if current_html is not None:
        dom_result = validate_bindings_against_html(context.form, bindings, current_html)
        if not dom_result.valid:
            return dom_result
    return result
