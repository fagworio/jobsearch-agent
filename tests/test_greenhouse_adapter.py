from pathlib import Path

import pytest

from jobsearch_agent.application import evaluate_safety_gate
from jobsearch_agent.ats import GreenhouseAdapter, adapter_for, inspect_with_adapter
from jobsearch_agent.inspector import InspectionError
from jobsearch_agent.models import ApplicationContext, ApplicationPolicy, ApplicationState
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/greenhouse"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_greenhouse_adapter_uses_deterministic_signature_and_root():
    html = _html("simple.html")
    adapter = GreenhouseAdapter()
    assert adapter.matches("https://boards.greenhouse.io/acme/jobs/1", html)
    assert adapter.confidence("https://boards.greenhouse.io/acme/jobs/1", html) == 1.0
    assert adapter.locate_application_root(html) == "#application_form"
    result = adapter.inspect(html, "https://boards.greenhouse.io/acme/jobs/1")
    assert result.provider == "greenhouse"
    assert result.confidence == 1.0
    assert result.form.provider == "greenhouse"
    assert result.form.source == "greenhouse_adapter"
    assert result.bindings.root_locator == "#application_form"
    assert adapter.allowed_hosts("https://boards.greenhouse.io/acme/jobs/1") == {"boards.greenhouse.io"}


def test_greenhouse_adapter_excludes_hidden_and_submit_controls():
    result = inspect_with_adapter(_html("simple.html"), "https://boards.greenhouse.io/acme/jobs/1")
    keys = {field.key for field in result.form.fields}
    assert "job_application[token]" not in keys
    assert all(field.field_type != "submit" for field in result.form.fields)
    by_type = {field.semantic_type: field for field in result.form.fields}
    assert by_type["first_name"].confidence == 1.0
    assert by_type["last_name"].confidence == 1.0
    assert by_type["email"].confidence == 1.0


def test_greenhouse_semantic_mapping_covers_files_and_sensitive_questions():
    resume = inspect_with_adapter(_html("resume-upload.html"), "https://boards.greenhouse.io/acme/jobs/1")
    assert {field.semantic_type for field in resume.form.fields} == {"resume", "cover_letter"}
    assert next(field for field in resume.form.fields if field.semantic_type == "resume").field_type == "file"

    sponsorship = inspect_with_adapter(_html("sponsorship.html"), "https://boards.greenhouse.io/acme/jobs/1")
    semantics = {field.semantic_type for field in sponsorship.form.fields}
    assert {"work_authorization", "requires_sponsorship"} <= semantics
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"requires_sponsorship": "yes", "work_authorization": ["Brazil"]})
    kb = AnswerKnowledgeBase([])
    for field in sponsorship.form.fields:
        field.answer = kb.resolve_field(field, profile, preferences)
    assert next(field for field in sponsorship.form.fields if field.semantic_type == "work_authorization").answer.answer == "Yes"
    assert next(field for field in sponsorship.form.fields if field.semantic_type == "requires_sponsorship").answer.answer == "Yes, I will require sponsorship"

    eeoc = inspect_with_adapter(_html("eeoc.html"), "https://boards.greenhouse.io/acme/jobs/1")
    assert {field.semantic_type for field in eeoc.form.fields} == {"gender", "race_ethnicity", "veteran_status", "disability"}
    assert all(field.value in ("", None) and field.answer is None for field in eeoc.form.fields)


def test_greenhouse_unknown_custom_question_stays_unknown_and_blocks_safety_gate():
    result = inspect_with_adapter(_html("custom-question.html"), "https://boards.greenhouse.io/acme/jobs/1")
    custom = next(field for field in result.form.fields if field.key == "job_application[question_12345]")
    assert custom.semantic_type == "unknown"
    assert custom.confidence == 0.0
    assert "custom_combobox" in result.unsupported_features
    assert result.warnings == ["required field has no high-confidence semantic mapping"]

    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", profile.preferences)
    assert AnswerKnowledgeBase([]).resolve_field(custom, profile, preferences) is None
    context = ApplicationContext(
        application_id="application-greenhouse",
        job_id="job-greenhouse",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        form=result.form,
        policy=ApplicationPolicy(),
    )
    readiness = evaluate_safety_gate(context)
    assert readiness.decision == ApplicationState.NEEDS_ANSWER
    assert "unknown_answer:job_application[question_12345]" in readiness.blockers


def test_greenhouse_checkbox_and_conditional_fields_remain_generic():
    checkbox = inspect_with_adapter(_html("checkbox-group.html"), "https://boards.greenhouse.io/acme/jobs/1")
    assert checkbox.form.fields[0].field_type == "checkbox"
    assert checkbox.form.fields[0].semantic_type == "checkbox_multi"
    assert checkbox.form.fields[0].options == ["Remote", "São Paulo"]

    conditional = inspect_with_adapter(_html("conditional.html"), "https://boards.greenhouse.io/acme/jobs/1")
    assert next(field for field in conditional.form.fields if field.semantic_type == "relocation").key == "job_application[relocation]"
    assert any(field.semantic_type == "unknown" for field in conditional.form.fields)


def test_greenhouse_adapter_rejects_ambiguous_or_non_greenhouse_roots():
    adapter = GreenhouseAdapter()
    with pytest.raises(InspectionError, match="GREENHOUSE_SIGNATURE_NOT_FOUND"):
        adapter.inspect('<form id="application_form"><input name="name"></form>', "https://example.com/apply")
    ambiguous = """
    <form data-provider="greenhouse"><input name="job_application[first_name]"></form>
    <form data-provider="greenhouse"><input name="job_application[last_name]"></form>
    """
    with pytest.raises(InspectionError, match="AMBIGUOUS_GREENHOUSE_APPLICATION_ROOT"):
        adapter.inspect(ambiguous, "")


def test_adapter_registry_does_not_guess_unknown_provider():
    assert adapter_for("https://example.com/apply", '<form data-provider="unknown"></form>') is None
