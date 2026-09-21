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
