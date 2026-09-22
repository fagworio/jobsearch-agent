from pathlib import Path

import pytest

from jobsearch_agent.application import evaluate_safety_gate
from jobsearch_agent.browser import BrowserSessionError, DryRunBrowserExecutor, validate_navigation_url
from jobsearch_agent.execution import ExecutionAction, ExecutionPlan, ExecutionPlanError, build_execution_plan
from jobsearch_agent.forms import validate_application_field, validate_application_form
from jobsearch_agent.inspector import ATSInspector
from jobsearch_agent.models import ApplicationContext, ApplicationField, ApplicationForm, ApplicationPolicy, ApplicationState
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


ROOT = Path(__file__).parents[1]


def _valid_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode())
        body.extend(obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    body.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode())
    body.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(body)


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
    artifact.write_bytes(_valid_pdf())
    valid = ApplicationField("resume", "Resume", field_type="file", semantic_type="resume", required=True, attachment_path=str(artifact), accepted_types=["pdf"])
    assert validate_application_field(valid, str(tmp_path)).valid is True
    value_only = ApplicationField("resume", "Resume", field_type="file", required=True, value=str(artifact), accepted_types=["pdf"])
    assert validate_application_field(value_only, str(tmp_path)).valid is False
    wrong = tmp_path / "resume.txt"
    wrong.write_text("fixture", encoding="utf-8")
    invalid_type = ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(wrong), accepted_types=["pdf"])
    assert validate_application_field(invalid_type, str(tmp_path)).valid is False
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(_valid_pdf())
    external = ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(outside), accepted_types=["pdf"])
    assert validate_application_field(external, str(tmp_path)).valid is False


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


def test_disabled_required_field_is_inactive_not_missing_answer():
    form = ApplicationForm("form-1", fields=[ApplicationField("conditional", "Conditional", required=True, disabled=True)])
    result = validate_application_form(form)
    assert result.valid is True
    assert result.details["field_results"]["conditional"]["code"] == "INACTIVE_FIELD"
    assert evaluate_safety_gate(_ready_context(form)).decision == ApplicationState.READY_TO_APPLY


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
    assert "invalid_option:auth" in readiness.blockers
    with pytest.raises(ExecutionPlanError):
        build_execution_plan(context)


def test_execution_plan_and_dry_run_have_no_submit_path(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(_valid_pdf())
    form = ApplicationForm(
        "greenhouse-1",
        provider="greenhouse",
        fields=[
            ApplicationField("name", "Name", required=True, value="Candidate"),
            ApplicationField("resume", "Resume", field_type="File", semantic_type="resume", required=True, attachment_path=str(artifact), accepted_types=["pdf"]),
        ],
        artifact_root=str(tmp_path),
    )
    context = _ready_context(form)
    plan = build_execution_plan(context)
    assert plan.final_action == "STOP_BEFORE_SUBMIT"
    assert [action.action_type for action in plan.actions] == ["fill", "upload"]
    executor = DryRunBrowserExecutor()
    result = executor.execute(context, plan)
    assert result.stopped_before_submit is True
    assert [item["operation"] for item in result.operations] == ["fill", "upload"]
    assert not hasattr(executor, "submit")


def test_execution_plan_detects_changed_upload(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    artifact.write_bytes(_valid_pdf())
    form = ApplicationForm("form-1", provider="greenhouse", artifact_root=str(tmp_path), fields=[ApplicationField("resume", "Resume", field_type="file", required=True, attachment_path=str(artifact), accepted_types=["pdf"])])
    context = _ready_context(form)
    plan = build_execution_plan(context)
    artifact.write_bytes(artifact.read_bytes() + b"changed")
    with pytest.raises(BrowserSessionError, match="hash"):
        DryRunBrowserExecutor().execute(context, plan)


def test_executor_rechecks_safety_gate_and_current_form():
    context = _ready_context(ApplicationForm("form-1", provider="greenhouse", fields=[ApplicationField("name", "Name", required=True, value="Candidate")]))
    bypassed = ExecutionPlan("application-1", "greenhouse", [ExecutionAction("fill", "name", "invented")])
    with pytest.raises(BrowserSessionError, match="refusing invalid execution plan"):
        DryRunBrowserExecutor().execute(context, bypassed)
    unknown = ExecutionPlan("application-1", "greenhouse", [ExecutionAction("fill", "other", "Candidate")])
    with pytest.raises(BrowserSessionError, match="refusing invalid execution plan"):
        DryRunBrowserExecutor().execute(context, unknown)


def test_navigation_policy_rejects_local_and_non_web_urls():
    assert validate_navigation_url("file:///etc/passwd").valid is False
    assert validate_navigation_url("http://127.0.0.1:8080").valid is False
    assert validate_navigation_url("http://169.254.169.254/latest/meta-data").valid is False
    assert validate_navigation_url("https://example.com:8443").valid is False
    assert validate_navigation_url("https://8.8.8.8", {"example.com"}).valid is False
    assert validate_navigation_url("https://8.8.8.8").valid is True


def test_inspector_separates_domain_form_from_dom_bindings():
    html = """
    <form data-provider="greenhouse">
      <label for="name">Full name</label><input id="name" name="name" required>
      <label>Consent <input id="consent" type="checkbox" required></label>
      <label><input name="skills" type="checkbox" value="Python">Python</label>
      <label><input name="skills" type="checkbox" value="SQL">SQL</label>
      <label for="resume">Resume</label><input id="resume" type="file" accept="application/pdf">
      <label for="country">Country</label><select id="country" name="country"><option value="br">Brazil</option><option value="us">United States</option></select>
    </form>
    """
    inspected = ATSInspector().inspect_html(html, "https://boards.greenhouse.io/example")
    assert inspected.form.provider == "greenhouse"
    by_key = {field.key: field for field in inspected.form.fields}
    assert by_key["consent"].semantic_type == "checkbox_boolean"
    assert by_key["skills"].semantic_type == "checkbox_multi"
    assert by_key["skills"].options == ["Python", "SQL"]
    assert by_key["resume"].field_type == "file"
    assert by_key["resume"].accepted_types == ["application/pdf"]
    assert inspected.bindings.for_field("country").locator == "#country"
    assert inspected.bindings.for_field("country").option_locators["Brazil"] == '#country option[value="br"]'
    assert not hasattr(inspected.form.fields[0], "locator")


def test_radio_option_bindings_are_unique_without_ids():
    inspected = ATSInspector().inspect_html("""
      <label>Yes <input name="authorization" type="radio" value="yes"></label>
      <label>No <input name="authorization" type="radio" value="no"></label>
    """)
    binding = inspected.bindings.for_field("authorization")
    assert binding.option_locators["Yes"] != binding.option_locators["No"]
    assert binding.option_values == {"Yes": "yes", "No": "no"}
