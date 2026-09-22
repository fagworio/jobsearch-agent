from pathlib import Path

from jobsearch_agent.linkedin.inspector import LinkedInInspector
from jobsearch_agent.models import ApplicationField
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase


ROOT = Path(__file__).parents[1]


def test_linkedin_experience_years_resolves_from_structured_skill_years():
    html = (ROOT / "tests/fixtures/linkedin/easy-apply-single.html").read_text(encoding="utf-8")
    form = LinkedInInspector().inspect_html(html).form
    field = next(item for item in form.fields if item.semantic_type == "experience_years")
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", profile.preferences)
    answer = AnswerKnowledgeBase([]).resolve_field(field, profile, preferences)
    assert answer is not None
    assert answer.answer == "5"
    assert answer.supported_by == ["CareerProfile.skills.wordpress.years"]
    assert answer.semantic_type == "experience_years"


def test_unknown_linkedin_experience_skill_stays_unresolved():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    field = ApplicationField(
        key="react_years",
        label="How many years of React experience?",
        semantic_type="experience_years",
        semantic_context={"skill": "React"},
        confidence=1.0,
    )
    assert AnswerKnowledgeBase([]).resolve_field(field, profile) is None
