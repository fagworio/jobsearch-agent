import json
from pathlib import Path

from jobsearch_agent.analysis import analyze_requirements, build_strategy, calculate_fit, detect_language
from jobsearch_agent.models import Job
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.skills import SkillRegistry
from jobsearch_agent.sources import JOBSPY, canonical_job_key, normalize_payload


ROOT = Path(__file__).parents[1]


def fixture(name):
    return json.loads((ROOT / "tests/fixtures/jobs" / name).read_text())


def test_known_adapters_normalize_and_preserve_identity():
    greenhouse = fixture("greenhouse.json")
    job = normalize_payload(greenhouse, greenhouse["absolute_url"])
    assert job.source == "greenhouse"
    assert job.external_id == "12345"
    assert job.company == "Example Labs"
    assert canonical_job_key(job) == "source:greenhouse:12345"

    lever = fixture("lever.json")
    assert normalize_payload(lever, lever["hostedUrl"]).source == "lever"
    ashby = fixture("ashby.json")
    assert normalize_payload(ashby, ashby["jobUrl"]).source == "ashby"


def test_generic_json_ld_and_language_override():
    job = normalize_payload({"@type": "JobPosting", "title": "Dev", "hiringOrganization": {"name": "Co"}, "description": "Experiência com WordPress."}, "https://example.invalid/jobs/1")
    assert job.company == "Co"
    assert detect_language("Experiência com desenvolvimento e responsabilidades").locale == "pt-BR"
    assert detect_language("We need experience with development and responsibilities").locale == "en-US"
    assert SkillRegistry.load().matches("RESTful APIs", "REST API")
    assert JOBSPY.can_handle("", {"source": "jobspy"})
    jobspy_job = normalize_payload({"source": "jobspy", "site": "indeed", "id": "1", "title": "Engineer", "company": "Co", "description": "Build APIs", "job_url": "https://example.invalid/jobs/1"})
    assert jobspy_job.source == "jobspy"


def test_requirements_fit_and_strategy_do_not_promote_missing_preferred_skills():
    payload = fixture("greenhouse.json")
    job = normalize_payload(payload, payload["absolute_url"])
    analysis = analyze_requirements(job)
    assert "Docker" in analysis.preferred_skills
    assert "Docker" not in analysis.required_skills
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    fit = calculate_fit(job, analysis, profile)
    strategy = build_strategy(job, analysis, fit, profile)
    assert "WordPress" in fit.matched_skills
    assert "Docker" in fit.missing_preferred
    assert "Docker" not in strategy.keywords
    assert strategy.secondary == ["REST API"]
    assert strategy.deprioritize == []


def test_language_override_normalizes_language_and_locale():
    assert detect_language("", "en-US").language == "en"
    assert detect_language("", "en-US").locale == "en-US"
    assert detect_language("", "pt-BR").language == "pt"


def test_location_and_language_fit_use_profile_capabilities():
    payload = fixture("greenhouse.json")
    job = normalize_payload(payload, payload["absolute_url"])
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    analysis = analyze_requirements(job)
    fit = calculate_fit(job, analysis, profile)
    assert fit.language_match == 1.0
    assert fit.location_match == 1.0


def test_configured_provider_can_enrich_structured_requirements_without_free_text_claims():
    class Provider:
        def complete(self, request):
            assert "Do not invent" in request.system
            return {"required_skills": ["WordPress"], "preferred_skills": ["Docker"], "responsibilities": ["Build plugins"], "seniority": "senior", "domain": "platform"}

    payload = fixture("greenhouse.json")
    job = normalize_payload(payload, payload["absolute_url"])
    analysis = analyze_requirements(job, Provider())
    assert analysis.required_skills == ["WordPress"]
    assert analysis.preferred_skills == ["Docker"]
    assert "Semantic fields" in analysis.explanation[-1]


def test_explicit_secondary_language_is_structured_and_explained():
    job = Job(id="j", source="fixture", external_id="1", company="Co", title="Desenvolvedor", description="Experiência com WordPress. Inglês avançado obrigatório para comunicação.", location="Remote")
    analysis = analyze_requirements(job)
    assert len(analysis.language_requirements) == 1
    assert analysis.language_requirements[0].language == "en"
    assert analysis.language_requirements[0].minimum_level == "advanced"
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    profile.languages["english"] = {"level": "basic"}
    fit = calculate_fit(job, analysis, profile)
    assert "language_mismatch" in fit.blockers
    assert any(item.criterion == "language:en" and item.blocker for item in fit.criteria)


def test_preferences_file_overrides_legacy_profile_preferences():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"remote": False, "allowed_locations": ["Canada"]})
    assert preferences.remote is True
    assert preferences.allowed_locations == ["Canada"]
    profile.candidate_preferences = preferences
    job = Job(id="j", source="fixture", external_id="1", company="Co", title="Engineer", description="Build software", location="Remote", remote_type="remote")
    analysis = analyze_requirements(job)
    assert calculate_fit(job, analysis, profile).location_match == 1.0


def test_work_authorization_requirement_is_separate_from_candidate_answer():
    job = Job(id="j", source="fixture", external_id="1", company="Co", title="Engineer", description="Candidates must be legally authorized to work in the United States.")
    analysis = analyze_requirements(job)
    assert analysis.work_authorization_requirement.required is True
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    fit_without_answer = calculate_fit(job, analysis, profile)
    assert "work_authorization_unknown" in fit_without_answer.blockers
    profile.candidate_preferences = load_preferences(ROOT / "profile/preferences.yaml", {"work_authorization": ["United States"]})
    fit_with_answer = calculate_fit(job, analysis, profile)
    assert "work_authorization_unknown" not in fit_with_answer.blockers
    assert "work_authorization_mismatch" not in fit_with_answer.blockers
