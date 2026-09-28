from __future__ import annotations

from job_agent_v2.answers import AnswerLibrary, resolve
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
