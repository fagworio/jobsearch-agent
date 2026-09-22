from dataclasses import replace
from pathlib import Path

from jobsearch_agent.models import CandidatePreferences
from jobsearch_agent.profile import load_preferences, load_profile, validate_profile_readiness
from jobsearch_agent.schemas import CandidatePreferencesSchema, ProfileSchema


ROOT = Path(__file__).parents[1]


def test_identity_and_preferences_contract_fields_are_optional():
    profile = ProfileSchema.model_validate({"identity": {"name": "Test Candidate"}})
    preferences = CandidatePreferencesSchema.model_validate({})
    assert profile.identity.first_name is None
    assert profile.identity.country is None
    assert preferences.timezone is None


def test_profile_readiness_reports_missing_explicit_paths_without_echoing_values():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", profile.preferences)
    readiness = validate_profile_readiness(profile, preferences)
    assert readiness.ready is True
    assert readiness.blockers == []
    assert readiness.missing_optional == [
        "identity.phone",
        "identity.linkedin",
        "identity.github",
        "preferences.timezone",
    ]


def test_profile_readiness_requires_explicit_values_and_accepts_single_timezone_preference():
    original = load_profile(ROOT / "profile/career_profile.yaml")
    profile = replace(original, identity=dict(original.identity), demo=False)
    profile.identity.update(
        phone="+1 555 0100",
        linkedin="https://www.linkedin.com/in/test-candidate",
        github="https://github.com/test-candidate",
    )
    preferences = CandidatePreferences(timezones=["America/Toronto"])
    readiness = validate_profile_readiness(profile, preferences)
    assert readiness.ready is True
    assert readiness.blockers == []
    assert readiness.missing_optional == []


def test_profile_readiness_requires_explicit_core_identity_fields():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    profile.identity.pop("first_name")
    readiness = validate_profile_readiness(profile)
    assert readiness.ready is False
    assert readiness.blockers == ["identity.first_name"]


def test_timezone_preference_loader_does_not_derive_timezone_from_location(tmp_path):
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text("locations:\n  allowed_locations: [Toronto]\n", encoding="utf-8")
    preferences = load_preferences(preferences_path)
    assert preferences.timezone == ""
    assert preferences.timezones == []
