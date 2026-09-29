from __future__ import annotations

import hashlib
import json

from job_agent_v2.discovery import (
    BatchReport,
    DiscoveryJob,
    DiscoveryMatch,
    DiscoveryMatrix,
    DiscoveryResults,
    DiscoverySearchRun,
    build_pipeline,
    plan_batch,
    rank_shortlist,
)


def _job(job_id: str, title: str, location: str, *, applied: bool = False) -> DiscoveryJob:
    return DiscoveryJob(
        provider="greenhouse",
        job_id=job_id,
        title=title,
        company="Example Co",
        href=f"https://my.greenhouse.io/jobs/example/{job_id}",
        remote=True,
        work_type="Remote",
        location=location,
        salary=None,
        posted="Posted",
        status="Applied" if applied else "Posted",
        applied=applied,
        viewed=False,
    )


def _matrix() -> DiscoveryMatrix:
    jobs = (
        _job("br:1", "Senior WordPress Developer", "Southeast, BR"),
        _job("us:2", "Senior WordPress Developer", "United States"),
        _job("br:3", "Senior WordPress Developer", "Southeast, BR", applied=True),
    )
    results = DiscoveryResults("greenhouse", "search", "document", "https://my.greenhouse.io/jobs/search", "MyGreenhouse", True, "wordpress", ("remote",), jobs)
    return DiscoveryMatrix(
        "greenhouse",
        ("remote",),
        (DiscoverySearchRun("primary", "wordpress", results),),
        tuple(
            DiscoveryMatch(job.job_id, 60.0, "GOOD", ("Wordpress",), ("wordpress",), ("wordpress",), ("title match",), "title/query")
            for job in jobs
        ),
        "profile/career_profile.local.yaml",
    )


def test_shortlist_ranks_and_justifies_without_accepting_unknown_geo():
    report = rank_shortlist(_matrix())
    by_id = {entry.job_id: entry for entry in report.entries}
    assert by_id["br:1"].selection == "APPROVED"
    assert by_id["us:2"].selection == "REVIEW"
    assert by_id["br:3"].selection == "REJECTED"
    assert by_id["br:1"].rank == 1
    assert any("Brazil" in reason for reason in by_id["br:1"].justification)
    assert any("review" in reason.lower() for reason in by_id["us:2"].justification)


def test_pipeline_emits_only_approved_jobs_for_all_three_current_actions():
    report = rank_shortlist(_matrix())
    manifest = build_pipeline(report.to_dict())
    assert [item.job_id for item in manifest.items] == ["br:1"]
    payload = manifest.items[0].to_dict()
    assert set(payload["commands"]) == {"apply", "fill", "submit"}
    assert payload["commands"]["fill"]["url"].endswith("/br:1")


def test_batch_keeps_fill_and_submit_manual_under_current_policy():
    manifest = build_pipeline(rank_shortlist(_matrix()).to_dict())
    policy = {"providers": {"greenhouse": {"fill_forms": "review", "submit": "manual"}}}
    fill_report = plan_batch(manifest, mode="fill", policy=policy)
    submit_report = plan_batch(manifest, mode="submit", policy=policy)
    assert isinstance(fill_report, BatchReport)
    assert fill_report.items[0].state == "MANUAL_REVIEW_REQUIRED"
    assert submit_report.items[0].state == "MANUAL_CONFIRMATION_REQUIRED"


def test_pipeline_excludes_jobs_with_submission_markers(tmp_path):
    report = rank_shortlist(_matrix())
    url = next(entry.url for entry in report.entries if entry.job_id == "br:1")
    marker = tmp_path / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.json"
    marker.write_text(json.dumps({"outcome": "SUBMIT_FAILED"}), encoding="utf-8")
    manifest = build_pipeline(report.to_dict(), submission_store=tmp_path)
    assert manifest.items == ()


def test_partial_fit_in_eligible_latam_is_approved_for_readiness_evaluation():
    job = _job("latam:partial", "Full Stack Web Engineer", "Bogota, CO")
    results = DiscoveryResults("greenhouse", "search", "document", "https://my.greenhouse.io/jobs/search", "MyGreenhouse", True, "web engineer", ("remote",), (job,))
    matrix = DiscoveryMatrix(
        "greenhouse",
        ("remote",),
        (DiscoverySearchRun("web", "web engineer", results),),
        (DiscoveryMatch("latam:partial", 42.0, "PARTIAL", ("Php",), ("web",), ("web engineer",), ("title role match",), "test"),),
        "test",
    )
    entry = rank_shortlist(matrix).entries[0]
    assert entry.fit_decision == "APPROVED"
    assert entry.selection == "APPROVED"
    assert entry.application_readiness == "PENDING_INSPECTION"
