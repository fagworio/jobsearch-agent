"""Perfis declarativos por provider de ATS."""

from __future__ import annotations

import pytest

from jobsearch_agent.ats import ADAPTERS, LeverAdapter, adapter_for
from jobsearch_agent.providers import (
    ProviderError,
    apply_url,
    profile_for,
    submit_destination,
    supported_providers,
)
from jobsearch_agent.submission import LiveNetworkPolicy, build_submission_payload
from jobsearch_agent.models import ApplicationField, ApplicationForm


def test_every_registered_adapter_has_a_profile():
    for adapter in ADAPTERS:
        assert profile_for(adapter.provider).provider == adapter.provider


def test_supported_providers_are_the_three_boards():
    assert supported_providers() == ["ashby", "greenhouse", "lever"]


def test_apply_url_appends_the_form_route_once():
    job = "https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1"
    assert apply_url("lever", job) == job + "/apply"
    assert apply_url("lever", job + "/apply") == job + "/apply"
    # Greenhouse serve o formulario na propria URL da vaga.
    assert apply_url("greenhouse", "https://job-boards.greenhouse.io/x/jobs/1") == "https://job-boards.greenhouse.io/x/jobs/1"


def test_submit_destination_matches_the_real_endpoints():
    job = "https://jobs.lever.co/spotify/2193db3f-77c5-43b8-b030-8f92c9882bf1"
    assert submit_destination("lever", job, "spotify", "2193db3f") == job + "/apply"
    assert submit_destination("greenhouse", "https://x/1", "canonical", "5150422") == "https://boards.greenhouse.io/canonical/jobs/5150422"
    with pytest.raises(ProviderError):
        submit_destination("greenhouse", "https://x/1", "", "5150422")
    with pytest.raises(ProviderError):
        submit_destination("ashby", "https://x/1", "ramp", "abc")


@pytest.mark.parametrize("provider", ["greenhouse", "lever"])
def test_live_network_policy_comes_from_the_profile(provider: str):
    profile = profile_for(provider)
    policy = LiveNetworkPolicy.for_submission(provider, "application-1", "intent-1")
    assert policy.allowed_origin == profile.submit_origin
    assert policy.allowed_path_pattern == profile.submit_path_pattern
    assert policy.allowed_method == profile.submit_method


def test_ashby_is_declared_but_not_submittable_yet():
    profile = profile_for("ashby")
    # O formulario dele so existe apos um POST na API.
    assert profile.form_loaded_by_api_write is True
    # Sem politica de submit declarada, a boundary recusa em vez de tentar.
    with pytest.raises(ValueError, match="no live network policy"):
        LiveNetworkPolicy.for_submission("ashby", "a", "i")


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderError):
        profile_for("workday")


# --- disambiguacao Greenhouse x Lever (mesmo id de form) ----------------------


LEVER_HTML = '<form id="application-form"><input name="resume"><input name="org"><input name="email"></form>'
GREENHOUSE_LEGACY = '<form id="application_form"><input name="job_application[first_name]"></form>'


def test_adapter_choice_prefers_the_url_host():
    assert adapter_for("https://jobs.lever.co/acme/abc-123/apply", LEVER_HTML).provider == "lever"
    assert adapter_for("https://job-boards.greenhouse.io/acme/jobs/1", LEVER_HTML).provider == "greenhouse"


def test_shared_form_id_does_not_steal_the_other_provider():
    # Sem host reconhecivel, a assinatura de nomes decide.
    assert adapter_for("https://careers.example.com/apply", LEVER_HTML).provider == "lever"
    assert adapter_for("https://careers.example.com/apply", GREENHOUSE_LEGACY).provider == "greenhouse"


def test_lever_adapter_maps_the_real_wire_names():
    html = """<form id="application-form">
      <input name="name" required><input name="email" required><input name="phone" required>
      <input name="location" required><input name="org" required>
      <input name="urls[LinkedIn]"><input name="urls[GitHub]">
      <input type="file" name="resume" id="resume-upload-input">
    </form>"""
    result = LeverAdapter().inspect(html, "https://jobs.lever.co/acme/abc-123/apply")
    semantics = {field.key: field.semantic_type for field in result.form.fields}
    assert semantics["name"] == "full_name"
    assert semantics["email"] == "email"
    assert semantics["phone"] == "phone"
    assert semantics["location"] == "current_location"
    assert semantics["org"] == "current_company"
    assert semantics["urls[LinkedIn]"] == "linkedin"
    assert semantics["urls[GitHub]"] == "github"
    assert semantics["resume"] == "resume"
    assert result.provider == "lever"


def test_lever_payload_keys_are_not_namespaced():
    """Lever ja publica os nomes de wire; so o Greenhouse usa namespace."""
    form = ApplicationForm(
        form_id="application-form",
        provider="lever",
        fields=[ApplicationField(key="email", label="Email", value="a@b.test", required=True)],
    )
    payload = build_submission_payload(form)
    assert payload.fields == {"email": "a@b.test"}
