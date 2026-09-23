import json
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.cli import main
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import Database


ROOT = Path(__file__).parents[1]


@pytest.mark.requires_libreoffice
def test_cli_help_and_full_fixture_run(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    capsys.readouterr()
    result = main([
        "run", "--json-file", str(ROOT / "tests/fixtures/jobs/greenhouse.json"),
        "--db", str(tmp_path / "jobs.db"), "--artifacts", str(tmp_path / "artifacts"),
        "--profile", str(ROOT / "profile/career_profile.yaml"), "--facts", str(ROOT / "profile/locked_facts.yaml"),
    ])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    artifact_dir = Path(output["prepared"]["artifacts"])
    assert output["prepared"]["validation"]["state"] == "RESUME_READY"
    assert all((artifact_dir / name).exists() for name in ("job.json", "resume.txt", "resume.docx", "resume.pdf", "validation.json"))


def test_real_profile_flag_rejects_demo(tmp_path, capsys):
    result = main([
        "profile", "validate", "--real-profile", "--profile", str(ROOT / "profile/career_profile.yaml"),
        "--facts", str(ROOT / "profile/locked_facts.yaml"), "--db", str(tmp_path / "jobs.db"),
    ])
    assert result == 2
    assert "demo profile" in capsys.readouterr().out


def test_submission_commands_are_exposed_without_adding_submit_to_dry_run(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["application", "--help"])
    assert exit_info.value.code == 0
    application_help = capsys.readouterr().out
    assert "review" in application_help
    assert "authorize-submit" in application_help
    assert "submit" in application_help


def test_cli_review_persists_snapshot_before_authorize(tmp_path, capsys):
    db_path = tmp_path / "jobs.db"
    db = Database(db_path)
    job = Job(
        id="job-cli-review",
        source="greenhouse",
        external_id="cli-review",
        company="Acme",
        title="Engineer",
        description="Build software",
    )
    db.save_job(job, "greenhouse:cli-review", {})
    application = ApplicationService(db).create_for_job(job.id)
    service = ApplicationService(db)
    service.transition(application.id, ApplicationState.PREPARING, "prepare")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "ready")
    db.close()

    review_result = main([
        "application", "review", application.id,
        "--provider", "greenhouse",
        "--destination", "https://boards.greenhouse.io/acme/jobs/cli-review",
        "--form-fingerprint", "form-v1",
        "--resume-sha256", "resume-v1",
        "--answers-fingerprint", "answers-v1",
        "--db", str(db_path),
    ])
    assert review_result == 0
    review_output = json.loads(capsys.readouterr().out)
    intent_id = review_output["submission_intent"]["id"]
    assert review_output["review_snapshot"]["destination"].endswith("/cli-review")
    assert review_output["review_snapshot"]["form_fingerprint"] == "form-v1"
    assert review_output["review_snapshot"]["answers_fingerprint"] == "answers-v1"

    authorize_result = main([
        "application", "authorize-submit", intent_id,
        "--db", str(db_path),
    ])
    assert authorize_result == 0
    authorize_output = json.loads(capsys.readouterr().out)
    assert authorize_output["submission_intent"]["status"] == "AUTHORIZED"


def test_dry_run_command_requires_explicit_local_snapshot(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["dry-run", "--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--html-file" in help_text
    assert "snapshot HTML local" in help_text


def test_linkedin_inspect_cli_uses_local_html_only(tmp_path, capsys):
    result = main([
        "linkedin", "inspect", "job-linkedin-fixture",
        "--html-file", str(ROOT / "tests/fixtures/linkedin/easy-apply-single.html"),
        "--db", str(tmp_path / "jobs.db"),
    ])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["job_id"] == "job-linkedin-fixture"
    assert output["classification"] == "EASY_APPLY"
    assert output["network_access"] == "none"
