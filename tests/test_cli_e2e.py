import json
from pathlib import Path

import pytest

from jobsearch_agent.cli import main


ROOT = Path(__file__).parents[1]


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
