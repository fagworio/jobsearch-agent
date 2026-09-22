from pathlib import Path

from jobsearch_agent.models import Job, Resume, ResumeClaim
from jobsearch_agent.llm import LLMError
from jobsearch_agent.profile import load_facts, load_profile, validate_facts as validate_profile_facts
from jobsearch_agent.resume import cluster_fact_ids, validate_ats, validate_facts
from jobsearch_agent.analysis import analyze_requirements, build_strategy, calculate_fit
from jobsearch_agent.resume import generate_resume, select_fact_ids
from jobsearch_agent.sources import normalize_payload
import json


ROOT = Path(__file__).parents[1]


def test_demo_profile_and_locked_facts_are_valid():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    assert profile.demo is True
    assert validate_profile_facts(profile, facts) == []


def test_identity_fact_reference_must_exist():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    profile.identity_fact_ids["first_name"] = "missing_identity_fact"
    errors = validate_profile_facts(profile, facts)
    assert "identity references unknown fact: missing_identity_fact" in errors


def test_identity_fact_reference_requires_identity_type():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    profile.identity_fact_ids["first_name"] = "fact_demo_001"
    errors = validate_profile_facts(profile, facts)
    assert "identity fact must have type identity: fact_demo_001" in errors


def test_unsupported_claim_is_blocked():
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    resume = Resume(
        id="r", job_id="j", language="en-US", header={"name": "Candidate"}, summary="summary",
        skills=[], experience=[{"role": "Developer", "company": "Co", "bullets": ["Improved conversion by 35%"], "fact_ids": ["fact_demo_001"]}],
        claims=[ResumeClaim("Improved conversion by 35%", ["fact_demo_001"])],
    )
    result = validate_facts(resume, facts)
    assert not result.valid
    assert result.code == "RESUME_VALIDATION_FAILED"
    assert result.details["unsupported_claim_atoms"]


def test_validator_rejects_unfounded_metric_even_when_words_overlap():
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    resume = Resume(
        id="r", job_id="j", language="en-US", header={"name": "Candidate"}, summary="summary",
        skills=[], experience=[{"role": "Developer", "company": "Co", "bullets": ["Developed WordPress plugins that increased revenue by 80%"], "fact_ids": ["fact_demo_001"]}],
        claims=[ResumeClaim("Developed WordPress plugins that increased revenue by 80%", ["fact_demo_001"])],
    )
    result = validate_facts(resume, facts)
    assert not result.valid
    assert "number:80%" in result.details["unsupported_claim_atoms"][0]["atoms"]


def test_ats_validator_requires_contact_and_experience():
    result = validate_ats(Resume(id="r", job_id="j", language="en-US", header={}, summary="", skills=[], experience=[]))
    assert not result.valid
    assert len(result.errors) >= 3


def test_dynamic_rewrite_changes_positioning_but_keeps_fact_support():
    payload = json.loads((ROOT / "tests/fixtures/jobs/greenhouse.json").read_text())
    job = normalize_payload(payload, payload["absolute_url"])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    analysis = analyze_requirements(job)
    strategy = build_strategy(job, analysis, calculate_fit(job, analysis, profile), profile)
    resume = generate_resume(job, strategy, profile, facts, select_fact_ids(job, strategy, profile, facts))
    assert len(resume.experience[0]["bullets"]) == 2
    assert set(resume.experience[0]["fact_ids"]) == {"fact_demo_001", "fact_demo_002"}
    assert validate_facts(resume, facts).valid


def test_fact_clustering_preserves_every_selected_id():
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    clusters = cluster_fact_ids(["fact_demo_001", "fact_demo_002"], facts)
    assert sorted(item for cluster in clusters for item in cluster) == ["fact_demo_001", "fact_demo_002"]


def test_summary_requires_its_own_fact_support():
    payload = json.loads((ROOT / "tests/fixtures/jobs/greenhouse.json").read_text())
    job = normalize_payload(payload, payload["absolute_url"])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    profile.summary_fact_ids = {}
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    analysis = analyze_requirements(job)
    strategy = build_strategy(job, analysis, calculate_fit(job, analysis, profile), profile)
    resume = generate_resume(job, strategy, profile, facts, select_fact_ids(job, strategy, profile, facts))
    result = validate_facts(resume, facts)
    assert not result.valid
    assert any("unsupported claim" in error for error in result.errors)


def test_llm_error_uses_deterministic_rewrite_and_records_fallback():
    class FailingProvider:
        def complete(self, request):
            raise LLMError("timeout")

    payload = json.loads((ROOT / "tests/fixtures/jobs/greenhouse.json").read_text())
    job = normalize_payload(payload, payload["absolute_url"])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    facts = load_facts(ROOT / "profile/locked_facts.yaml")
    analysis = analyze_requirements(job)
    strategy = build_strategy(job, analysis, calculate_fit(job, analysis, profile), profile)
    fallback_events = []
    resume = generate_resume(job, strategy, profile, facts, select_fact_ids(job, strategy, profile, facts), FailingProvider(), fallback_events)
    assert resume.experience
    assert fallback_events
    assert all(event["reason"] == "llm_error" for event in fallback_events)
    assert validate_facts(resume, facts).valid
