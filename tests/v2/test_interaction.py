from job_agent_v2.answers import AnswerLibrary
from job_agent_v2.facts import FactStore
from job_agent_v2.interaction import collect_pending_questions


def test_collect_pending_canonical_fact_and_specific_answer():
    facts = FactStore()
    library = AnswerLibrary()
    answers = iter(["2", "Yes"])
    output = []
    count = collect_pending_questions(
        [
            {"fact_id": "employment.notice_period", "question": "When can you start?", "options": ["Immediately", "30 days"]},
            {"fact_id": "question:have you worked at acme?", "question": "Have you worked at Acme?", "options": ["Yes", "No"]},
        ],
        facts=facts,
        library=library,
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
    )
    assert count == 2
    assert facts.get("employment.notice_period").value == "30 days"
    assert library.get("Have you worked at Acme?") == "Yes"


def test_collect_pending_can_persist_each_answer_immediately():
    facts = FactStore()
    library = AnswerLibrary()
    saves = []
    answers = iter(["30 days", "No"])
    collect_pending_questions(
        [
            {"fact_id": "employment.notice_period", "question": "When can you start?", "options": ["Immediately", "30 days"]},
            {"fact_id": "question:have you worked at acme?", "question": "Have you worked at Acme?", "options": ["Yes", "No"]},
        ],
        facts=facts,
        library=library,
        input_fn=lambda _prompt: next(answers),
        output_fn=lambda _line: None,
        save_fn=lambda: saves.append((facts.as_dict(), library.as_dict())),
    )
    assert len(saves) == 2
    assert "employment.notice_period" in saves[0][0]
    assert "have you worked at acme?" in saves[1][1]
