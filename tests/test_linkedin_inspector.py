from pathlib import Path

from jobsearch_agent.linkedin.inspector import LinkedInApplyClassification, LinkedInInspector


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/linkedin"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_easy_apply_fixture_is_classified_and_inspected_read_only():
    result = LinkedInInspector().inspect_html(_html("easy-apply-single.html"))
    assert result.classification == LinkedInApplyClassification.EASY_APPLY
    assert result.form is not None
    assert result.form.provider == "linkedin"
    assert result.form.source == "linkedin_fixture_inspector"
    assert result.bindings is not None
    assert {field.semantic_type for field in result.form.fields} >= {"email", "phone", "resume", "experience_years"}
    experience = next(field for field in result.form.fields if field.semantic_type == "experience_years")
    assert experience.semantic_context == {"skill": "WordPress"}


def test_linkedin_classification_covers_external_already_unavailable_and_auth_states():
    inspector = LinkedInInspector()
    assert inspector.classify_html(_html("external-apply.html")) == LinkedInApplyClassification.EXTERNAL_APPLY
    assert inspector.classify_html(_html("already-applied.html")) == LinkedInApplyClassification.ALREADY_APPLIED
    assert inspector.classify_html(_html("unavailable.html")) == LinkedInApplyClassification.UNAVAILABLE
    assert inspector.classify_html(_html("login.html")) == LinkedInApplyClassification.AUTH_REQUIRED
    assert inspector.classify_html(_html("mfa.html")) == LinkedInApplyClassification.AUTH_REQUIRED
    assert inspector.classify_html(_html("captcha.html")) == LinkedInApplyClassification.AUTH_REQUIRED
    assert inspector.inspect_html(_html("login.html")).auth_state.value == "NEEDS_LOGIN"
    assert inspector.inspect_html(_html("mfa.html")).auth_state.value == "NEEDS_MFA"
    assert inspector.inspect_html(_html("captcha.html")).auth_state.value == "NEEDS_CAPTCHA"


def test_unknown_page_is_unavailable_and_does_not_create_a_form():
    result = LinkedInInspector().inspect_html(_html("unsupported.html"))
    assert result.classification == LinkedInApplyClassification.UNAVAILABLE
    assert result.form is None
    assert result.bindings is None
    assert "apply control" in " ".join(result.warnings).casefold()
