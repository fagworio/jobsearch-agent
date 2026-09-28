from __future__ import annotations

from job_agent_v2.config import V2Config
from job_agent_v2.profile import load_profile


def test_config_reads_only_v2_timeout():
    config = V2Config.from_environment({"JOB_AGENT_V2_TIMEOUT_MS": "1234"})
    assert config.timeout_ms == 1234
    assert config.native_host_name == "com.job_agent_v2"


def test_profile_is_explicit_json(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text('{"Email": "user@example.test", "years": 12}', encoding="utf-8")
    assert load_profile(path) == {"Email": "user@example.test", "years": "12"}
