import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_hermes_skill_has_safety_invariants():
    content = (ROOT / "hermes/SKILL.md").read_text()
    assert "Analyze + Generate" in content
    assert "must not submit" in content
    assert "locked fact" in content
    assert "CAPTCHA/MFA" in content


def test_hermes_installer_is_valid_and_idempotent_in_shape():
    script = ROOT / "hermes/install.sh"
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    content = script.read_text()
    assert "mkdir -p" in content
    assert "cp \"$ROOT/hermes/SKILL.md\"" in content
    assert "SKILL_NAME=\"jobsearch-agent\"" in content

