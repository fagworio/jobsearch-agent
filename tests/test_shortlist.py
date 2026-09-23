"""Seleção explicável de vagas aderentes ao perfil."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jobsearch_agent.models import CareerProfile, Experience, Job
from jobsearch_agent.shortlist import (
    DEFAULT_EXCLUDE_TERMS,
    ShortlistCriteria,
    evaluate_job,
    shortlist,
    summary,
    terms_from_profile,
)


ROOT = Path(__file__).parents[1]


def _job(
    title: str,
    company: str = "acme",
    url: str = "https://boards.greenhouse.io/acme/jobs/1",
    location: str = "Remote",
) -> Job:
    return Job(
        id=f"job-{title}",
        source="greenhouse",
        external_id="1",
        company=company,
        title=title,
        description="Build web applications.",
        url=url,
        location=location,
        remote_type="remote" if "remote" in location.casefold() else "unknown",
    )


def _profile() -> CareerProfile:
    return CareerProfile(
        identity={"first_name": "Joao"},
        professional_summary={"en-US": "Front-end developer"},
        experiences=[Experience(id="e1", company="Acme", role="Frontend Developer", start_date="2020")],
        skills={"react": {"level": "advanced"}},
        languages={"english": {"level": "advanced"}},
    )


def test_high_score_on_an_unrelated_role_is_rejected():
    """O caso real: 'Senior Backend Engineer, Database Excellence' tirou 97.5."""
    entry = evaluate_job(
        _job("Senior Backend Engineer, Database Excellence"),
        score=97.5,
        required_skills=["SQL"],
        matched_skills=["SQL"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert not entry.accepted
    # Poucas skills extraídas barram antes mesmo do título.
    assert entry.reason.startswith("INSUFFICIENT_REQUIREMENTS")


def test_off_target_role_is_rejected_even_with_enough_skills():
    entry = evaluate_job(
        _job("Senior Backend Engineer, Database Excellence"),
        score=95.0,
        required_skills=["Python", "SQL", "AWS", "Kubernetes"],
        matched_skills=["SQL", "AWS"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert not entry.accepted
    assert entry.reason.startswith("OFF_TARGET_ROLE")
    assert "backend" in entry.reason


def test_frontend_role_with_enough_evidence_is_shortlisted():
    entry = evaluate_job(
        _job("Senior Frontend Engineer, Home Experience"),
        score=88.0,
        required_skills=["JavaScript", "React", "TypeScript", "CSS"],
        matched_skills=["JavaScript", "React", "TypeScript", "CSS"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert entry.accepted
    assert "frontend" in entry.reason


def test_target_term_overrides_an_excluded_discipline():
    """'Frontend Engineer, Payments & Risk' não pode ser barrado por 'risk'."""
    entry = evaluate_job(
        _job("Frontend Engineer, Mobile Web"),
        score=90.0,
        required_skills=["React", "JavaScript", "CSS"],
        matched_skills=["React", "JavaScript", "CSS"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert entry.accepted


def test_hard_fit_blockers_reject_the_job():
    entry = evaluate_job(
        _job("Frontend Engineer"),
        score=95.0,
        required_skills=["React", "Vue", "Angular"],
        matched_skills=["React"],
        blockers=["missing_required:Vue", "missing_required:Angular"],
        criteria=ShortlistCriteria(),
    )
    assert not entry.accepted
    assert entry.reason.startswith("FIT_BLOCKED")


def test_low_seniority_is_rejected_even_when_the_title_is_on_target():
    """'Frontend Engineer Intern' casa com 'frontend' mas é nível errado."""
    entry = evaluate_job(
        _job("Software Engineer Intern, Frontend (Summer 2026)"),
        score=97.5,
        required_skills=["React", "JavaScript", "CSS", "HTML"],
        matched_skills=["React", "JavaScript", "CSS", "HTML"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert not entry.accepted
    assert entry.reason.startswith("SENIORITY_MISMATCH")


def test_onsite_role_in_another_country_is_rejected():
    entry = evaluate_job(
        _job("Staff Software Engineer, Web", location="Mountain View, California"),
        score=93.0,
        required_skills=["React", "JavaScript", "CSS", "HTML"],
        matched_skills=["React", "JavaScript", "CSS", "HTML"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert not entry.accepted
    assert entry.reason.startswith("LOCATION_UNSUPPORTED")


def test_location_can_be_relaxed_explicitly():
    job = _job("Frontend Engineer", location="Mountain View, California")
    kwargs = dict(score=90.0, required_skills=["React", "CSS", "HTML"], matched_skills=["React"], blockers=[])
    assert not evaluate_job(job, criteria=ShortlistCriteria(), **kwargs).accepted
    relaxed = replace(ShortlistCriteria(), allow_location_mismatch=True)
    assert evaluate_job(job, criteria=relaxed, **kwargs).accepted


def test_scores_below_the_threshold_are_rejected_last():
    entry = evaluate_job(
        _job("Frontend Engineer"),
        score=55.0,
        required_skills=["React", "CSS", "HTML"],
        matched_skills=["React"],
        blockers=[],
        criteria=ShortlistCriteria(),
    )
    assert not entry.accepted
    assert entry.reason.startswith("BELOW_SCORE")


def test_company_limit_keeps_the_shortlist_diverse():
    jobs = [
        (_job("Frontend Engineer", company="gitlab", url="https://x/1"), 95.0, ["React", "CSS", "HTML"], ["React"], []),
        (_job("Frontend Developer", company="gitlab", url="https://x/2"), 94.0, ["React", "CSS", "HTML"], ["React"], []),
        (_job("Web Developer", company="stripe", url="https://x/3"), 93.0, ["React", "CSS", "HTML"], ["React"], []),
    ]
    result = shortlist(jobs, _profile(), ShortlistCriteria(max_per_company=1))
    assert [entry.job.company for entry in result.accepted] == ["gitlab", "stripe"]
    assert any(entry.reason.startswith("COMPANY_LIMIT") for entry in result.rejected)


def test_shortlist_orders_by_score_and_reports_reasons():
    jobs = [
        (_job("Web Developer", company="a"), 71.0, ["React", "CSS", "HTML"], ["React"], []),
        (_job("Frontend Engineer", company="b"), 93.0, ["React", "CSS", "HTML"], ["React"], []),
        (_job("Sales Manager", company="c"), 99.0, ["React", "CSS", "HTML"], ["React"], []),
    ]
    result = shortlist(jobs, _profile(), ShortlistCriteria())
    assert [entry.score for entry in result.accepted] == [93.0, 71.0]
    report = summary(result)
    assert report["considered"] == 3
    assert report["shortlisted"] == 2
    assert report["rejection_reasons"]["OFF_TARGET_ROLE"] == 1


def test_profile_roles_extend_the_target_terms():
    profile = CareerProfile(
        identity={},
        professional_summary={},
        experiences=[Experience(id="e", company="C", role="Shopify Developer | WordPress Developer", start_date="2020")],
        skills={},
        languages={},
    )
    terms = terms_from_profile(profile)
    assert "shopify developer" in terms
    assert "wordpress developer" in terms
    assert "frontend" in terms
    assert DEFAULT_EXCLUDE_TERMS
