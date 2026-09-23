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


def test_canada_authorization_mismatch_and_unavailable_sponsorship_block_fit():
    job = Job(
        id="canada-job",
        source="fixture",
        external_id="canada-1",
        company="Example",
        title="Engineer",
        description=(
            "Candidates must be legally authorized to work in Canada. "
            "We do not offer visa sponsorship."
        ),
    )
    analysis = analyze_requirements(job)
    assert analysis.work_authorization_requirement.countries == ["canada"]
    assert analysis.work_authorization_requirement.sponsorship_available == "not_available"
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    profile.candidate_preferences = load_preferences(
        ROOT / "profile/preferences.yaml",
        {"work_authorization": ["Brazil"], "requires_sponsorship": "no"},
    )
    mismatch = calculate_fit(job, analysis, profile)
    assert "work_authorization_mismatch" in mismatch.blockers

    profile.candidate_preferences = load_preferences(
        ROOT / "profile/preferences.yaml",
        {"work_authorization": ["Canada"], "requires_sponsorship": "yes"},
    )
    sponsorship_blocker = calculate_fit(job, analysis, profile)
    assert "sponsorship_unavailable" in sponsorship_blocker.blockers


def test_sponsorship_availability_is_separate_from_candidate_need():
    job = Job(id="j", source="fixture", external_id="1", company="Co", title="Engineer", description="Candidates must be legally authorized to work in the United States. We do not offer visa sponsorship.")
    analysis = analyze_requirements(job)
    assert analysis.work_authorization_requirement.sponsorship_available == "not_available"
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    profile.candidate_preferences = load_preferences(ROOT / "profile/preferences.yaml", {"work_authorization": ["United States"], "requires_sponsorship": "yes"})
    assert "sponsorship_unavailable" in calculate_fit(job, analysis, profile).blockers


def test_provider_payloads_with_pandas_scalars_stay_json_serializable():
    """JobSpy hands back pandas records: dates, numpy scalars and NaN."""
    import json
    from datetime import date, datetime
    from decimal import Decimal

    from jobsearch_agent.sources import _json_safe, normalize_payload

    payload = {
        "title": "WordPress Developer",
        "company": "Acme",
        "job_url": "https://example.test/jobs/1",
        "date_posted": date(2026, 9, 1),
        "updated_at": datetime(2026, 9, 2, 10, 30),
        "min_amount": Decimal("5000.0"),
        "max_amount": float("nan"),
        "nested": {"posted": date(2026, 8, 1)},
        "tags": [date(2026, 7, 1)],
    }
    job = normalize_payload(payload, payload["job_url"])

    assert job.posted_at == "2026-09-01"
    encoded = json.dumps(job.raw_payload)
    assert "2026-09-01" in encoded
    assert "NaN" not in encoded
    assert _json_safe(float("nan")) is None
    assert _json_safe(Decimal("12.5")) == "12.5"
    assert _json_safe({1: "a"}) == {"1": "a"}


def test_fuzzy_skill_extraction_rejects_partial_token_hits():
    """WRatio scores 'github' against 'github actions' at 90; that is not a skill."""
    from jobsearch_agent.skills import SkillRegistry

    registry = SkillRegistry.load(ROOT / "knowledge/skills.yaml")
    assert registry.extract("A GitHub profile is required for this role.") == []
    assert registry.extract("Build software for our customers.") == []
    # Exact and near-exact aliases still resolve.
    assert "CI/CD" in registry.extract("We run GitHub Actions and GitLab CI pipelines.")
    assert "Kubernetes" in registry.extract("Experience with Kubernetes and Helm.")


def test_fit_does_not_treat_missing_requirements_as_a_perfect_match():
    """A posting with no extractable requirement must not score as a full match."""
    from jobsearch_agent.skills import SkillRegistry

    registry = SkillRegistry.load(ROOT / "knowledge/skills.yaml")
    assert registry.extract("Build software for our customers.") == []
    job = Job(id="j", source="fixture", external_id="1", company="Co", title="Engineer", description="Build software for our customers.")
    analysis = analyze_requirements(job)
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    fit = calculate_fit(job, analysis, profile)
    assert fit.required_match == 0.0
    assert any(item.criterion == "required_skills" and item.result.value == "unknown" for item in fit.criteria)
    assert "no required skill was extracted" in " ".join(fit.explanation).casefold()


def test_fit_scores_a_matching_stack_above_an_unrelated_one():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    matching = Job(id="m", source="fixture", external_id="m", company="Co", title="WordPress Developer", description="WordPress, WooCommerce and PHP.")
    unrelated = Job(id="u", source="fixture", external_id="u", company="Co", title="DevOps Engineer", description="Kubernetes, Terraform, Go and Rust in production.")
    matching_fit = calculate_fit(matching, analyze_requirements(matching), profile)
    unrelated_fit = calculate_fit(unrelated, analyze_requirements(unrelated), profile)
    assert matching_fit.score > unrelated_fit.score
    assert "missing_required:Kubernetes" in unrelated_fit.blockers


def test_short_skill_tokens_never_fuzzy_match_by_substring():
    """`normalize("C#")` e "c", e similaridade parcial casaria "c" com "css"."""
    from jobsearch_agent.analysis import _skill_keys, _skill_matches
    from jobsearch_agent.profile import load_profile

    profile = load_profile(ROOT / "profile/career_profile.local.yaml")
    keys = _skill_keys(profile)
    # Tecnologias ausentes nao podem aparecer como presentes.
    assert _skill_matches("C#", keys) is False
    assert _skill_matches(".NET", keys) is False
    assert _skill_matches("Java", keys) is False
    assert _skill_matches("Python", keys) is False
    assert _skill_matches("Go", keys) is False
    # As reais continuam casando.
    for present in ("CSS", "React", "Angular", "TypeScript", "PHP", "Node.js"):
        assert _skill_matches(present, keys) is True, present
