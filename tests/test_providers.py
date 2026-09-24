"""Perfis declarativos por provider de ATS."""

from __future__ import annotations

from urllib.parse import urlsplit

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


# --- action declarado pelo formulario ----------------------------------------


def test_relative_form_action_resolves_to_an_absolute_destination():
    """`action="/apply"` nao pode chegar literal em create_intent."""
    job = "https://jobs.lever.co/acme/uuid"
    resolved = submit_destination("lever", job, "acme", "uuid", "/apply")
    parts = urlsplit(resolved)
    assert parts.scheme == "https" and parts.netloc == "jobs.lever.co"
    # `path-relative` fica no mesmo nivel da pagina.
    assert submit_destination("lever", job, "acme", "uuid", "apply") == job + "/apply"
    # absoluto e respeitado como veio
    absolute = "https://jobs.lever.co/acme/uuid/apply"
    assert submit_destination("lever", job, "acme", "uuid", absolute) == absolute
    # sem action, reconstroi
    assert submit_destination("lever", job, "acme", "uuid", "") == job + "/apply"


def test_form_action_and_method_survive_a_persistence_round_trip():
    from jobsearch_agent.application import context_from_dict
    from jobsearch_agent.models import to_dict

    original = ApplicationForm(
        form_id="application-form",
        provider="lever",
        fields=[ApplicationField(key="email", label="Email", value="a@b.test")],
        action="https://jobs.lever.co/acme/uuid/apply",
        method="POST",
    )
    stored = to_dict(original)
    assert stored["action"] == "https://jobs.lever.co/acme/uuid/apply"

    reloaded = context_from_dict({"form": stored}).form
    assert reloaded.action == original.action
    assert reloaded.method == original.method
    # e o destino derivado continua correto apos o reload
    assert submit_destination("lever", "https://jobs.lever.co/acme/uuid", "acme", "uuid", reloaded.action) == original.action


def test_reason_token_is_a_closed_set_of_domain_states():
    from jobsearch_agent.submission import SubmissionBoundaryError, _redacted_evidence, SubmissionVerification

    assert _redacted_evidence(SubmissionVerification.failed("x", reason_token="captcha_no_write"))["reason_token"] == "captcha_no_write"
    with pytest.raises(SubmissionBoundaryError, match="unsupported failure reason token"):
        _redacted_evidence(SubmissionVerification.failed("x", reason_token="anything_else"))


def test_lever_upload_permit_cannot_authorise_the_submission():
    """O upload do Lever vive na mesma origem do submit: so o caminho separa os dois.

    Sem a restricao por caminho, o permit de upload (`^/.*$`) autorizaria o POST
    de candidatura e a submissao deixaria de ser um evento unico autorizado.
    """
    from jobsearch_agent.browser import AuthorizedWrite

    profile = profile_for("lever")
    paths = dict(profile.upload_write_paths)
    permits = [
        AuthorizedWrite(
            application_id="app-1",
            submission_intent_id="",
            origin=host,
            path_pattern=paths.get(host, r"^/.*$"),
            method="POST",
            max_writes=1,
        )
        for host in profile.upload_write_origins
    ]
    submit_url = "https://jobs.lever.co/ciandt/59494544-d851-4267-b0d2-fd953d4d8a72/apply"
    assert not any(permit.covers("POST", submit_url) for permit in permits)
    assert any(permit.covers("POST", "https://jobs.lever.co/parseResume") for permit in permits)
    # Um POST de infraestrutura do Cloudflare nao pode consumir o credito do
    # curriculo, que era exatamente o defeito observado no board real.
    assert not any(
        permit.covers("POST", "https://jobs.lever.co/cdn-cgi/challenge-platform/h/b/jsd/oneshot/abc/0.6:1:x/y")
        for permit in permits
    )
    # O storage do board continua aceito em qualquer caminho (URL pre-assinada).
    assert any(permit.covers("POST", "https://acme.s3.amazonaws.com/upload/xyz") for permit in permits)
