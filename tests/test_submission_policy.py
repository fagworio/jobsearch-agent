"""Politica de canal: a trava de "sem credencial, sem API".

O teste central deste arquivo e o que impede um POST nao autorizado a um
terceiro: um dominio cuja API exige credencial NAO vira canal `API` enquanto a
credencial nao estiver declarada. Medido antes de escrito: o POST publico do
Greenhouse responde 401 "HTTP Basic: Access denied".
"""

from __future__ import annotations

import pytest

from jobsearch_agent.submission_policy import DEFAULT_POLICY, Channel, DomainPolicy


@pytest.mark.parametrize(
    "domain",
    ["greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "apply.workable.com", "jobs.ashbyhq.com"],
)
def test_without_a_credential_the_api_domain_degrades_to_the_browser(domain: str) -> None:
    """Sem credencial declarada, nunca `API` — degrada para o browser."""
    assert DEFAULT_POLICY.channel_for(domain) is Channel.BROWSER


@pytest.mark.parametrize(
    "domain",
    ["greenhouse.io", "boards.greenhouse.io", "jobs.lever.co"],
)
def test_with_a_credential_the_api_channel_is_allowed(domain: str) -> None:
    root = "greenhouse.io" if "greenhouse" in domain else "lever.co"
    assert DEFAULT_POLICY.channel_for(domain, authorized_domains=[root]) is Channel.API


def test_a_credential_for_another_provider_does_not_open_this_one() -> None:
    assert DEFAULT_POLICY.channel_for("greenhouse.io", authorized_domains=["lever.co"]) is Channel.BROWSER


@pytest.mark.parametrize("domain", ["linkedin.com", "www.linkedin.com", "indeed.com", "br.indeed.com"])
def test_linkedin_and_indeed_are_never_api(domain: str) -> None:
    """Nao ha API publica de candidatura — e automatizar apply ali e recusado."""
    assert DEFAULT_POLICY.channel_for(domain) is Channel.HANDOFF
    # Nem com credencial declarada por engano.
    assert DEFAULT_POLICY.channel_for(domain, authorized_domains=[domain]) is Channel.HANDOFF


def test_an_unknown_domain_falls_to_the_browser() -> None:
    assert DEFAULT_POLICY.channel_for("careers.acme.com") is Channel.BROWSER


def test_skip_is_explicit_and_wins_over_everything() -> None:
    policy = DomainPolicy(skip_domains=("hostile.example",))

    assert policy.channel_for("hostile.example", authorized_domains=["hostile.example"]) is Channel.SKIP


def test_subdomains_match_by_suffix_but_not_by_prefix() -> None:
    policy = DomainPolicy()

    assert policy.channel_for("boards.greenhouse.io", authorized_domains=["greenhouse.io"]) is Channel.API
    # "greenhouse.io.evil.example" nao e subdominio de greenhouse.io.
    assert policy.channel_for("greenhouse.io.evil.example") is Channel.BROWSER


def test_the_authorized_subset_is_reportable() -> None:
    authorized = DEFAULT_POLICY.api_domains_authorized(["greenhouse.io", "linkedin.com"])

    assert authorized == ("greenhouse.io",)


def test_the_policy_never_returns_api_for_a_domain_outside_the_table() -> None:
    for domain in ("acme.com", "boards.greenhouse.io", "careers.example.org"):
        channel = DEFAULT_POLICY.channel_for(domain, authorized_domains=["acme.com"])
        if domain != "boards.greenhouse.io":
            assert channel is not Channel.API
