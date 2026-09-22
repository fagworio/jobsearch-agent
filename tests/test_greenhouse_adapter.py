from pathlib import Path

import pytest

from jobsearch_agent.application import evaluate_safety_gate
from jobsearch_agent.ats import GreenhouseAdapter, adapter_for, inspect_with_adapter
from jobsearch_agent.inspector import InspectionError, validate_bindings_against_html
from jobsearch_agent.models import ApplicationContext, ApplicationField, ApplicationPolicy, ApplicationState, ApplicationForm, CandidatePreferences, FormCapabilityIssue
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
    assert validate_bindings_against_html(result.form, result.bindings, html, "https://boards.greenhouse.io/acme/jobs/1").valid
    assert adapter.allowed_hosts("https://boards.greenhouse.io/acme/jobs/1") == {
        "boards.greenhouse.io",
        "boards.cdn.greenhouse.io",
    }


def test_greenhouse_adapter_excludes_hidden_and_submit_controls():
    result = inspect_with_adapter(_html("simple.html"), "https://boards.greenhouse.io/acme/jobs/1")
    keys = {field.key for field in result.form.fields}
    assert "job_application[token]" not in keys
    assert all(field.field_type != "submit" for field in result.form.fields)
    by_type = {field.semantic_type: field for field in result.form.fields}
    assert by_type["first_name"].confidence == 1.0
    assert by_type["last_name"].confidence == 1.0
    assert by_type["email"].confidence == 1.0


def test_greenhouse_adapter_recognizes_modern_application_form_signature():
    result = inspect_with_adapter(_html("modern.html"), "https://job-boards.greenhouse.io/acme/jobs/1")
    assert result.form.provider == "greenhouse"
    assert result.bindings.root_locator == "#application-form"
    assert {field.semantic_type for field in result.form.fields} >= {"first_name", "last_name", "email"}
    assert result.form.fields[-1].semantic_type == "resume"


def test_greenhouse_identity_fields_use_explicit_profile_attributes():
    result = inspect_with_adapter(_html("simple.html"), "https://boards.greenhouse.io/acme/jobs/1")
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml")
    kb = AnswerKnowledgeBase([])

    first_name = next(field for field in result.form.fields if field.semantic_type == "first_name")
    last_name = next(field for field in result.form.fields if field.semantic_type == "last_name")
    first_answer = kb.resolve_field(first_name, profile, preferences)
    last_answer = kb.resolve_field(last_name, profile, preferences)
    assert first_answer.answer == "Demo"
    assert last_answer.answer == "Candidate"
    assert first_answer.supported_by == ["CareerProfile.identity.first_name", "fact_demo_identity_001"]
    assert last_answer.supported_by == ["CareerProfile.identity.last_name", "fact_demo_identity_002"]

    profile.identity.pop("first_name")
    profile.identity.pop("last_name")
    assert kb.resolve_field(first_name, profile, preferences) is None
    assert kb.resolve_field(last_name, profile, preferences) is None


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

    unsupported_html = _html("custom-question.html").replace("</form>", '<div role="combobox"></div></form>')
    unsupported = inspect_with_adapter(unsupported_html, "https://boards.greenhouse.io/acme/jobs/1")
    assert "custom_combobox" in unsupported.unsupported_features
    assert unsupported.form.capability_issues[0].severity == "blocker"


def test_unsupported_capability_issue_is_a_safety_gate_blocker():
    form = ApplicationForm(
        "form-unsupported",
        provider="greenhouse",
        fields=[ApplicationField("name", "Name", required=True, value="Candidate", confidence=1.0)],
        capability_issues=[FormCapabilityIssue("custom_combobox", "blocker", evidence="role=combobox")],
    )
    context = ApplicationContext(
        application_id="application-unsupported",
        job_id="job-unsupported",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        form=form,
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )
    readiness = evaluate_safety_gate(context)
    assert readiness.decision == ApplicationState.UNSUPPORTED_FORM
    assert "unsupported_form" in readiness.blockers
    assert any(check["gate"] == "capabilities" for check in readiness.checks)


def test_work_authorization_requires_jurisdiction_and_confidence():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"work_authorization": ["Brazil"]})
    kb = AnswerKnowledgeBase([])
    us = ApplicationField(
        "job_application[authorized_to_work]",
        "Are you authorized to work in the United States?",
        field_type="radio",
        semantic_type="work_authorization",
        options=["Yes", "No"],
        confidence=1.0,
        semantic_context={"country": "United States"},
    )
    assert kb.resolve_field(us, profile, preferences).answer == "No"
    unknown_country = ApplicationField(
        "job_application[authorized_to_work]",
        "Are you authorized to work?",
        field_type="radio",
        semantic_type="work_authorization",
        options=["Yes", "No"],
        confidence=1.0,
    )
    assert kb.resolve_field(unknown_country, profile, preferences) is None
    low_confidence = ApplicationField(
        "job_application[authorized_to_work]",
        "Are you authorized to work in Brazil?",
        field_type="radio",
        semantic_type="work_authorization",
        options=["Yes", "No"],
        confidence=0.70,
    )
    assert kb.resolve_field(low_confidence, profile, preferences) is None


def test_greenhouse_checkbox_and_conditional_fields_keep_explicit_semantics():
    checkbox = inspect_with_adapter(_html("checkbox-group.html"), "https://boards.greenhouse.io/acme/jobs/1")
    assert checkbox.form.fields[0].field_type == "checkbox"
    assert checkbox.form.fields[0].semantic_type == "checkbox_multi"
    assert checkbox.form.fields[0].options == ["Remote", "São Paulo"]

    conditional = inspect_with_adapter(_html("conditional.html"), "https://boards.greenhouse.io/acme/jobs/1")
    assert next(field for field in conditional.form.fields if field.semantic_type == "relocation").key == "job_application[relocation]"
    preferred = next(field for field in conditional.form.fields if field.key == "job_application[relocation_location]")
    assert preferred.semantic_type == "preferred_relocation_location"
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml")
    assert AnswerKnowledgeBase([]).resolve_field(preferred, profile, preferences) is None


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


def test_unsupported_controls_outside_application_root_are_ignored():
    for external_control in (
        '<div role="combobox" aria-label="job search"></div>',
        '<div contenteditable="true" aria-label="chat widget"></div>',
    ):
        html = _html("simple.html").replace("<body>", f"<body>{external_control}")
        result = inspect_with_adapter(html, "https://boards.greenhouse.io/acme/jobs/1")
        assert result.unsupported_features == []
        assert result.form.capability_issues == []


def test_greenhouse_single_combobox_is_bound_but_location_and_custom_controls_stay_blocked():
    html = _html("async-combobox.html")
    result = inspect_with_adapter(html, "https://job-boards.greenhouse.io/acme/jobs/1")
    field = next(item for item in result.form.fields if item.key == "job_application[country]")
    binding = result.bindings.for_field(field.key)
    assert field.field_type == "combobox"
    assert binding.control == "combobox"
    assert binding.listbox_id == "country-options"
    assert binding.autocomplete == "list"
    assert result.form.capability_issues == []

    location = html.replace('name="job_application[country]"', 'name="job_application[location]"')
    blocked = inspect_with_adapter(location, "https://job-boards.greenhouse.io/acme/jobs/1")
    assert any(issue.code == "combobox_location" for issue in blocked.form.capability_issues)

    multiselect = html.replace('aria-autocomplete="list"', 'aria-autocomplete="list" aria-multiselectable="true"')
    blocked_multi = inspect_with_adapter(multiselect, "https://job-boards.greenhouse.io/acme/jobs/1")
    assert any(issue.code == "combobox_multiple" for issue in blocked_multi.form.capability_issues)

    unsupported_autocomplete = html.replace('aria-autocomplete="list"', 'aria-autocomplete="inline"')
    blocked_autocomplete = inspect_with_adapter(unsupported_autocomplete, "https://job-boards.greenhouse.io/acme/jobs/1")
    assert any(issue.code == "combobox_autocomplete" for issue in blocked_autocomplete.form.capability_issues)

    custom = html.replace('<input id="country"', '<div id="country"')
    blocked_custom = inspect_with_adapter(custom, "https://job-boards.greenhouse.io/acme/jobs/1")
    assert any(issue.code == "custom_combobox" for issue in blocked_custom.form.capability_issues)


def test_phone_widget_internal_combobox_is_not_counted_as_an_application_field():
    html = _html("simple.html").replace(
        "</form>",
        '<input id="iti-0__search-input" class="iti__search-input" type="search" '
        'role="combobox" aria-label="Search" aria-controls="iti-0__country-listbox"></form>',
    )
    result = inspect_with_adapter(html, "https://boards.greenhouse.io/acme/jobs/1")
    assert "iti-0__search-input" not in {field.key for field in result.form.fields}
    assert result.form.capability_issues == []


def test_identity_and_contact_semantics_resolve_only_explicit_profile_values():
    additions = """
      <label for="preferred">Preferred First Name</label><input id="preferred" name="job_application[preferred_first_name]">
      <label for="country">Country</label><input id="country" name="job_application[country]" role="combobox" aria-autocomplete="list">
      <label for="phone">Phone</label><input id="phone" name="job_application[phone]" type="tel">
      <label for="location">Current Location</label><input id="location" name="job_application[current_location]">
      <label for="timezone">Time Zone</label><input id="timezone" name="job_application[time_zone]">
      <label for="linkedin">LinkedIn</label><input id="linkedin" name="job_application[linkedin]" type="url">
      <label for="github">GitHub</label><input id="github" name="job_application[github]" type="url">
      <label for="website">Personal Website</label><input id="website" name="job_application[website]" type="url">
    """
    html = _html("simple.html").replace("</form>", additions + "</form>")
    result = inspect_with_adapter(html, "https://boards.greenhouse.io/acme/jobs/1")
    semantics = {field.key: field.semantic_type for field in result.form.fields}
    assert semantics["job_application[preferred_first_name]"] == "preferred_first_name"
    assert semantics["job_application[country]"] == "country"
    assert semantics["job_application[time_zone]"] == "timezone"
    assert semantics["job_application[linkedin]"] == "linkedin"
    assert semantics["job_application[github]"] == "github"
    assert semantics["job_application[website]"] == "website"

    profile = load_profile(ROOT / "profile/career_profile.yaml")
    profile.identity.update({
        "preferred_first_name": "Rae",
        "phone": "+1 555 0100",
        "country": "Canada",
        "current_location": "Toronto, Canada",
        "linkedin": "https://www.linkedin.com/in/test-candidate",
        "github": "https://github.com/test-candidate",
        "website": "https://example.invalid",
    })
    preferences = CandidatePreferences(timezone="America/Toronto")
    kb = AnswerKnowledgeBase([])
    expected = {
        "job_application[preferred_first_name]": ("Rae", "CareerProfile.identity.preferred_first_name"),
        "job_application[country]": ("Canada", "CareerProfile.identity.country"),
        "job_application[phone]": ("+1 555 0100", "CareerProfile.identity.phone"),
        "job_application[current_location]": ("Toronto, Canada", "CareerProfile.identity.current_location"),
        "job_application[time_zone]": ("America/Toronto", "CandidatePreferences.timezone"),
        "job_application[linkedin]": ("https://www.linkedin.com/in/test-candidate", "CareerProfile.identity.linkedin"),
        "job_application[github]": ("https://github.com/test-candidate", "CareerProfile.identity.github"),
        "job_application[website]": ("https://example.invalid", "CareerProfile.identity.website"),
    }
    for key, (value, provenance) in expected.items():
        field = next(item for item in result.form.fields if item.key == key)
        answer = kb.resolve_field(field, profile, preferences)
        assert answer is not None
        assert answer.answer == value
        assert answer.supported_by == [provenance]
        assert answer.field_key == key


def test_preferred_name_country_location_and_timezone_are_never_derived():
    html = _html("simple.html").replace(
        "</form>",
        '<label for="preferred">Preferred First Name</label><input id="preferred" name="job_application[preferred_first_name]">'
        '<label for="country">Country</label><input id="country" name="job_application[country]">'
        '<label for="location">Current Location</label><input id="location" name="job_application[current_location]">'
        '<label for="timezone">Time Zone</label><input id="timezone" name="job_application[timezone]"></form>',
    )
    result = inspect_with_adapter(html, "https://boards.greenhouse.io/acme/jobs/1")
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = CandidatePreferences()
    kb = AnswerKnowledgeBase([])
    for key in (
        "job_application[preferred_first_name]",
        "job_application[country]",
        "job_application[current_location]",
        "job_application[timezone]",
    ):
        field = next(item for item in result.form.fields if item.key == key)
        assert kb.resolve_field(field, profile, preferences) is None
