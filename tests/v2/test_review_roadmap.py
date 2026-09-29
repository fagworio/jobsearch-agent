from __future__ import annotations

from job_agent_v2.facts import FactStore
from job_agent_v2.facts_migration import load_approved_answers, migrate_answers
from job_agent_v2.discovery import SearchBudget, SearchCursor
from job_agent_v2.questions import canonical_fact_for


def test_v1_migration_only_imports_registered_aliases():
    facts = FactStore()
    report = migrate_answers(
        {
            "When can you start?": "30 days",
            "Unknown personal question": "unsafe to classify",
        },
        facts,
    )
    assert report.to_dict() == {"migrated": 1, "already_present": 0, "ambiguous": 1, "rejected": 0}
    assert facts.get("employment.notice_period").value == "30 days"


def test_migration_does_not_overwrite_existing_canonical_fact():
    facts = FactStore()
    facts.remember("employment.notice_period", "60 days")
    report = migrate_answers({"When can you start?": "30 days"}, facts)
    assert report.already_present == 1
    assert facts.get("employment.notice_period").value == "60 days"


def test_search_cursor_stops_only_on_explicit_budget_or_target():
    budget = SearchBudget(target_ready_jobs=2, max_queries=3, max_jobs_inspected=20, max_pages=3)
    assert SearchCursor(queries_processed=0, jobs_inspected=0, pages=0, ready_jobs=0).should_continue(budget)
    assert not SearchCursor(queries_processed=3, jobs_inspected=4, pages=3, ready_jobs=0).should_continue(budget)
    assert not SearchCursor(queries_processed=1, jobs_inspected=4, pages=1, ready_jobs=2).should_continue(budget)


def test_legacy_yaml_loader_keeps_only_approved_answers(tmp_path):
    source = tmp_path / "answers.local.yaml"
    source.write_text(
        "answers:\n"
        "- question: What are your pronouns?\n"
        "  answer: Prefer not to say\n"
        "  approved: true\n"
        "- question: What is your notice period?\n"
        "  answer: 30 days\n"
        "  approved: false\n",
        encoding="utf-8",
    )
    assert load_approved_answers(source) == {"What are your pronouns?": "Prefer not to say"}


def test_provider_wrapped_aliases_are_deterministic():
    assert canonical_fact_for("Which of the following best describes your experience working in a digital agency or consulting firm?") == "experience.agency"
    assert canonical_fact_for("How much notice do you need to provide before you can start?") == "employment.notice_period"
    assert canonical_fact_for("In which country do you currently work?") == "identity.country"
