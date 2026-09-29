from __future__ import annotations

from job_agent_v2.answers import AnswerLibrary, resolve
from job_agent_v2.facts import FactStore
from job_agent_v2.models import Field, Form


def test_answer_library_is_explicit_and_reusable(tmp_path):
    path = tmp_path / "answers.json"
    library = AnswerLibrary()
    library.remember("Have you worked at Spotify?", "No")
    library.save(path)

    loaded = AnswerLibrary.load(path)
    form = Form((Field("q1", "Have you worked at Spotify?", options=("Yes", "No"), required=True, kind="radio"),))
    result = resolve(form, library=loaded)
    assert result.answers == {"q1": "No"}
    assert result.resolved_from == {"q1": "approved_answer_library"}
    assert result.missing == ()


def test_resolution_does_not_mutate_library():
    library = AnswerLibrary()
    form = Form((Field("q1", "Unknown question", required=True),))
    result = resolve(form, library=library)
    assert result.missing == form.fields
    assert library.as_dict() == {}


def test_canonical_fact_precedes_textual_library_and_preserves_provenance():
    facts = FactStore({
        "employment.notice_period": {"value": "30 days", "approved": True, "source": "user"},
    })
    library = AnswerLibrary({"When can you start?": "immediately"})
    form = Form((Field("q1", "When would you be able to begin?", required=True),))
    result = resolve(form, library=library, facts=facts)
    assert result.answers == {"q1": "30 days"}
    assert result.resolved_from == {"q1": "canonical_fact"}
    assert result.resolved_fact_ids == {"q1": "employment.notice_period"}


def test_unknown_semantic_question_stays_missing():
    facts = FactStore({"employment.notice_period": {"value": "30 days", "approved": True, "source": "user"}})
    form = Form((Field("q1", "Tell us something not in the registry", required=True),))
    result = resolve(form, facts=facts)
    assert result.missing == form.fields


def test_missing_question_preserves_field_and_options():
    form = Form((Field("q1", "How many years?", options=("0-2", "3+"), required=True, kind="radio"),))
    result = resolve(form)
    assert result.missing_questions[0].to_dict() == {
        "fact_id": "question:how many years?",
        "field_id": "q1",
        "question": "How many years?",
        "options": ["0-2", "3+"],
        "required": True,
    }


def test_canonical_fact_maps_to_one_matching_provider_option():
    facts = FactStore({"employment.notice_period": {"value": "immediate", "approved": True, "source": "user"}})
    form = Form((Field("q1", "When can you start?", options=("Immediately", "30 days"), required=True, kind="select"),))
    result = resolve(form, facts=facts)
    assert result.answers == {"q1": "Immediately"}
    assert result.complete


def test_canonical_fact_with_no_deterministic_option_stays_missing():
    facts = FactStore({"employment.notice_period": {"value": "immediate", "approved": True, "source": "user"}})
    form = Form((Field("q1", "When can you start?", options=("30 days", "More than 30 days"), required=True, kind="select"),))
    result = resolve(form, facts=facts)
    assert not result.complete
    assert result.missing_questions[0].fact_id == "employment.notice_period"


def test_year_fact_maps_to_one_numeric_range_option():
    facts = FactStore({"experience.wordpress_years": {"value": "12+", "approved": True, "source": "user"}})
    form = Form((Field("q1", "How many years of experience with WordPress?", options=("0-5 years", "10+ years"), required=True, kind="select"),))
    result = resolve(form, facts=facts)
    assert result.answers == {"q1": "10+ years"}
