from pathlib import Path

import pytest

from jobsearch_agent.application import evaluate_safety_gate
from jobsearch_agent.browser import DryRunBrowserExecutor
from jobsearch_agent.execution import ExecutionPlanError, build_execution_plan
from jobsearch_agent.forms import validate_application_field, validate_application_form
from jobsearch_agent.models import ApplicationContext, ApplicationField, ApplicationForm, ApplicationPolicy, ApplicationState
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


ROOT = Path(__file__).parents[1]


def _ready_context(form: ApplicationForm) -> ApplicationContext:
    return ApplicationContext(
        application_id="application-1",
        job_id="job-1",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        form=form,
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )


def test_file_field_requires_existing_artifact_and_accepted_extension(tmp_path: Path):
    missing = ApplicationField("resume", "Resume", field_type="file", semantic_type="resume", required=True, accepted_types=["pdf"])
    assert validate_application_field(missing).valid is False
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(b"fixture")
    valid = ApplicationField("resume", "Resume", field_type="file", semantic_type="resume", required=True, attachment_path=str(artifact), accepted_types=["pdf"])
    assert validate_application_field(valid).valid is True
    wrong = tmp_path / "resume.txt"
    wrong.write_text("fixture", encoding="utf-8")
    invalid_type = ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(wrong), accepted_types=["pdf"])
    assert validate_application_field(invalid_type).valid is False


def test_form_validation_rejects_unknown_option_and_required_checkbox():
    form = ApplicationForm(
        "form-1",
        fields=[
            ApplicationField("sponsorship", "Sponsorship", field_type="radio", options=["Yes", "No"], required=True, value="Brazil"),
            ApplicationField("consent", "Consent", field_type="checkbox", semantic_type="checkbox_boolean", required=True, value="false"),
        ],
    )
    result = validate_application_form(form)
    assert result.valid is False
    assert set(result.details["field_errors"]) == {"sponsorship", "consent"}


def test_checkbox_multi_requires_selected_known_options():
    empty = ApplicationField("skills", "Skills", field_type="checkbox", semantic_type="checkbox_multi", multiple=True, required=True, options=["Python", "SQL"], value=[])
    assert validate_application_field(empty).valid is False
    selected = ApplicationField("skills", "Skills", field_type="checkbox", semantic_type="checkbox_multi", multiple=True, required=True, options=["Python", "SQL"], value=["Python"])
    assert validate_application_field(selected).valid is True
    unknown = ApplicationField("skills", "Skills", field_type="checkbox", semantic_type="checkbox_multi", multiple=True, options=["Python", "SQL"], value=["Rust"])
    assert validate_application_field(unknown).valid is False


def test_sponsorship_answer_uses_exact_available_option():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"requires_sponsorship": "yes"})
    field = ApplicationField(
        "sponsorship",
        "Will you require sponsorship?",
        field_type="radio",
        semantic_type="requires_sponsorship",
        options=["Yes, I will require sponsorship", "No, I will not require sponsorship"],
    )
    answer = AnswerKnowledgeBase([]).resolve_field(field, profile, preferences)
    assert answer is not None
    assert answer.answer == "Yes, I will require sponsorship"


def test_safety_gate_rejects_invalid_option_before_execution():
    context = _ready_context(ApplicationForm("form-1", fields=[ApplicationField("auth", "Authorization", field_type="select", required=True, options=["Yes", "No"], value="Brazil")]))
    readiness = evaluate_safety_gate(context)
    assert readiness.decision == ApplicationState.NEEDS_ANSWER
    assert "unknown_answer:auth" in readiness.blockers
    with pytest.raises(ExecutionPlanError):
        build_execution_plan(context)


def test_execution_plan_and_dry_run_have_no_submit_path(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(b"fixture")
    form = ApplicationForm(
        "greenhouse-1",
        provider="greenhouse",
        fields=[
            ApplicationField("name", "Name", required=True, value="Candidate"),
            ApplicationField("resume", "Resume", field_type="file", semantic_type="resume", required=True, attachment_path=str(artifact), accepted_types=["pdf"]),
        ],
    )
    plan = build_execution_plan(_ready_context(form))
    assert plan.final_action == "STOP_BEFORE_SUBMIT"
    assert [action.action_type for action in plan.actions] == ["fill", "upload"]
    executor = DryRunBrowserExecutor()
    result = executor.execute(plan)
    assert result.stopped_before_submit is True
    assert [item["operation"] for item in result.operations] == ["fill", "upload"]
    assert not hasattr(executor, "submit")
