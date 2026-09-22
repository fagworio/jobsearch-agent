import json
from pathlib import Path

import yaml

from jobsearch_agent.cli import build_parser, main
from jobsearch_agent.config import Settings
from jobsearch_agent.models import Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.pipeline import precheck_job
from jobsearch_agent.sources import canonical_job_key


ROOT = Path(__file__).parents[1]


def _real_profile_settings(tmp_path, database_path, *, preferences=None):
    source = yaml.safe_load((ROOT / "profile/career_profile.yaml").read_text(encoding="utf-8"))
    source["demo"] = False
    source["identity"].update(first_name="Test", last_name="Candidate", email="test@example.invalid")
    profile_path = tmp_path / "candidate.yaml"
    profile_path.write_text(yaml.safe_dump(source), encoding="utf-8")
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(yaml.safe_dump(preferences or {}), encoding="utf-8")
    return Settings.from_args(
        ROOT,
        db=str(database_path),
        profile=str(profile_path),
        preferences=str(preferences_path),
    )


def _store_job(database_path, description):
    job = Job(
        id="test-job",
        source="greenhouse",
        external_id="test-job",
        company="Example",
        title="Engineer",
        description=description,
    )
    database = Database(database_path)
    database.save_job(job, canonical_job_key(job), {"id": job.external_id})
    database.close()
    return job


def test_precheck_blocks_demo_candidate_with_unknown_canada_authorization_without_mutation(tmp_path):
    database_path = tmp_path / "jobs.db"
    job = _store_job(
        database_path,
        (
            "Candidates must be legally authorized to work in Canada. "
            "We do not offer visa sponsorship."
        ),
    )

    settings = Settings.from_args(ROOT, db=str(database_path))
    result = precheck_job(settings, job.id)
    assert result["decision"] == "DEMO_PROFILE_BLOCKED"
    assert result["ready_for_dry_run"] is False
    assert result["browser_started"] is False
    assert result["fit"]["status"] == "UNKNOWN"
    assert result["profile"]["status"] == "NEEDS_DATA"
    assert "work_authorization:Canada" in result["profile"]["missing_required"]
    assert result["policy"]["blockers"] == ["DEMO_PROFILE_BLOCKED"]
    assert "identity.phone" in result["profile"]["missing_optional"]
    assert "url" not in result["job"]

    database = Database(database_path)
    persisted = database.get_job(job.id)
    assert persisted.state == job.state
    assert database.get_application_for_job(job.id) is None
    assert database.connection.execute("SELECT COUNT(*) FROM analyses WHERE job_id=?", (job.id,)).fetchone()[0] == 0
    database.close()


def test_profile_readiness_cli_is_redacted_and_reports_demo_profile(capsys):
    exit_code = main(["--root", str(ROOT), "profile", "readiness"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["profile_kind"] == "demo"
    assert payload["public_dry_run_allowed"] is False
    assert payload["missing_required"] == []
    assert payload["missing_optional"] == [
        "identity.phone",
        "identity.linkedin",
        "identity.github",
        "preferences.timezone",
    ]
    assert "Demo Candidate" not in json.dumps(payload)


def test_precheck_cli_contract_requires_an_ingested_job_id():
    args = build_parser().parse_args(["precheck", "job-demo"])
    assert args.handler == "precheck"
    assert args.job_id == "job-demo"


def test_missing_optional_github_only_blocks_when_posting_explicitly_requires_it(tmp_path):
    database_path = tmp_path / "jobs.db"
    job = _store_job(database_path, "Build software for our customers.")
    settings = _real_profile_settings(tmp_path, database_path)

    result = precheck_job(settings, job.id)

    assert result["decision"] == "READY"
    assert result["fit"]["status"] == "MATCH"
    assert "identity.github" in result["profile"]["missing_optional"]


def test_missing_optional_github_blocks_when_posting_explicitly_requires_it(tmp_path):
    database_path = tmp_path / "jobs.db"
    job = _store_job(database_path, "A GitHub profile is required for this role.")
    settings = _real_profile_settings(tmp_path, database_path)

    result = precheck_job(settings, job.id)

    assert result["decision"] == "NEEDS_PROFILE_DATA"
    assert result["profile"]["status"] == "NEEDS_DATA"
    assert "identity.github" in result["profile"]["missing_required"]


def test_unknown_work_authorization_is_profile_data_not_fit_incompatibility(tmp_path):
    database_path = tmp_path / "jobs.db"
    job = _store_job(database_path, "Must be legally authorized to work in Canada.")
    settings = _real_profile_settings(tmp_path, database_path)

    result = precheck_job(settings, job.id)

    assert result["decision"] == "NEEDS_PROFILE_DATA"
    assert result["fit"]["status"] == "UNKNOWN"
    assert result["fit"]["blockers"] == []
    assert result["profile"]["missing_required"] == ["work_authorization:Canada"]


def test_explicitly_incompatible_work_authorization_is_fit_blocker(tmp_path):
    database_path = tmp_path / "jobs.db"
    job = _store_job(database_path, "Must be legally authorized to work in Canada.")
    settings = _real_profile_settings(tmp_path, database_path, preferences={"work_authorization": ["Brazil"]})

    result = precheck_job(settings, job.id)

    assert result["decision"] == "BLOCKED_FIT"
    assert result["fit"]["status"] == "BLOCKED"
    assert "work_authorization_mismatch" in result["fit"]["blockers"]


def test_explicitly_matching_work_authorization_passes_fit_gate(tmp_path):
    database_path = tmp_path / "jobs.db"
    job = _store_job(database_path, "Must be legally authorized to work in Canada.")
    settings = _real_profile_settings(tmp_path, database_path, preferences={"work_authorization": ["Canada"]})

    result = precheck_job(settings, job.id)

    assert result["fit"]["status"] == "MATCH"
    assert "work_authorization_unknown" not in result["fit"]["blockers"]
