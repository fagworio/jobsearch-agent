from __future__ import annotations

import pytest

from job_agent_v2.auto_apply import AutoApplyItem, AutoApplyReport, _approved_items, _is_hard_failure, _validate_limits
from job_agent_v2.submit import _refusal
from job_agent_v2.discovery.pipeline import PipelineItem, PipelineManifest


def _manifest() -> PipelineManifest:
    return PipelineManifest(
        "shortlist.json",
        tuple(
            PipelineItem(rank, f"job:{rank}", f"https://example.test/{rank}", f"Job {rank}", "Example", 50.0)
            for rank in (3, 1, 2)
        ),
    )


def test_batch_selection_is_ranked_and_budgeted():
    selected = _approved_items(_manifest(), 2)
    assert [item.job_id for item in selected] == ["job:1", "job:2"]


def test_batch_budget_requires_sequential_execution():
    _validate_limits(max_jobs=5, max_submits=3, max_failures=2, parallelism=1)
    with pytest.raises(ValueError, match="parallelism"):
        _validate_limits(max_jobs=5, max_submits=3, max_failures=2, parallelism=2)


def test_unknown_submission_is_not_a_retryable_failure():
    assert _is_hard_failure("SUBMIT_FAILED")
    assert not _is_hard_failure("SUBMIT_UNKNOWN")


def test_reconciliation_refusal_does_not_consume_submit_budget(tmp_path):
    report = _refusal({"outcome": "SUBMITTED", "job_url": "https://example.test/job"}, tmp_path / "marker.json")
    assert report.state.value == "REFUSED"
    assert report.attempts == 0
    assert report.submission_writes == 0


def test_pending_questions_are_deduplicated_and_keep_job_context():
    question = {
        "fact_id": "employment.notice_period",
        "field_id": "q1",
        "question": "When can you start?",
        "options": ["Immediately", "30 days"],
        "required": True,
    }
    report = AutoApplyReport(
        "pipeline.json", "auto-apply", 5, 3, 2, 1,
        (
            AutoApplyItem("a", "Role A", "Acme", "https://a", "NEEDS_INPUT", missing_facts=("employment.notice_period",), missing_questions=(question,)),
            AutoApplyItem("b", "Role B", "Beta", "https://b", "NEEDS_INPUT", missing_facts=("employment.notice_period",), missing_questions=(question,)),
        ),
    )
    payload = report.to_dict()
    assert payload["pending_questions_count"] == 1
    assert [item["job_id"] for item in payload["pending_questions"][0]["used_by"]] == ["a", "b"]
