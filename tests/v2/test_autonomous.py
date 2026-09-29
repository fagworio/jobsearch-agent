from __future__ import annotations

import json

import yaml

from job_agent_v2.autonomous import run_autonomous


class FakeDiscoveryBrowser:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def inspect_discovery_results(self):
        return self._snapshot("frontend", [])

    def discover_query(self, query, _work_type):
        return self._snapshot(query, [{
            "provider": "greenhouse",
            "job_id": "example:1",
            "title": "Frontend Engineer",
            "company": "Example",
            "href": "https://job-boards.greenhouse.io/example/jobs/1",
            "remote": True,
            "work_type": "remote",
            "location": "Remote",
            "salary": None,
            "posted": "today",
            "status": "open",
            "applied": False,
            "viewed": False,
        }])

    @staticmethod
    def _snapshot(query, jobs):
        return {
            "provider": "greenhouse",
            "page_type": "search",
            "surface": "mygreenhouse",
            "url": f"https://my.greenhouse.io/jobs/search?query={query}",
            "title": "Jobs",
            "ready": True,
            "query": query,
            "work_type": ["remote"],
            "jobs": jobs,
        }


def test_run_creates_one_persisted_plan_without_submitting(monkeypatch, tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({
        "identity": {"name": "Candidate", "email": "candidate@example.test"},
        "experience": [{"id": "exp-1", "company": "Example", "role": "Frontend Engineer", "facts": []}],
        "skills": {"frontend": {"tags": ["frontend"]}, "javascript": {"tags": ["javascript"]}},
    }), encoding="utf-8")
    policy = tmp_path / "policy.yaml"
    policy.write_text("providers:\n  greenhouse:\n    submit: manual\n", encoding="utf-8")
    monkeypatch.setattr("job_agent_v2.autonomous.NativeMessagingClient", FakeDiscoveryBrowser)

    report = run_autonomous(
        profile_path=str(profile),
        policy_path=str(policy),
        matrix_path=tmp_path / "matrix.json",
        shortlist_path=tmp_path / "shortlist.json",
        pipeline_path=tmp_path / "pipeline.json",
        state_path=tmp_path / "state.json",
        target_ready_jobs=1,
        max_queries=1,
        max_jobs_inspected=10,
        max_pages=1,
    )

    assert report.state == "PLANNED"
    assert report.approved == 1
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["state"] == "PLANNED"
    pipeline = json.loads((tmp_path / "pipeline.json").read_text(encoding="utf-8"))
    assert pipeline["count"] == 1
