from pathlib import Path

from jobsearch_agent.browser import NetworkRequestEvent
from jobsearch_agent.preflight import run_preflight


ROOT = Path(__file__).parents[1]


class FakePage:
    url = "https://boards.greenhouse.io/acme/jobs/1"
    frames = []

    def content(self):
        return (ROOT / "tests/fixtures/greenhouse/simple.html").read_text(encoding="utf-8")

    def evaluate(self, *_args, **_kwargs):
        return True


class FakeNetworkGuard:
    pending_read_count = 0

    def __init__(self):
        self.events = [
            NetworkRequestEvent("https://cdn.example.com", "hash", "GET", "script", False, "host not allowed"),
            NetworkRequestEvent("https://boards.greenhouse.io", "hash", "POST", "fetch", False, "write method blocked in dry-run"),
        ]


class FakeSession:
    def __init__(self, **_kwargs):
        self.page = FakePage()
        self.network_guard = FakeNetworkGuard()
        self.guarded = False

    def start(self):
        self.guarded = True

    def open(self, _url):
        return None

    def close(self):
        self.guarded = False


def test_public_preflight_is_read_only_and_persists_redacted_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr("jobsearch_agent.preflight.PlaywrightSessionManager", FakeSession)
    result = run_preflight("https://boards.greenhouse.io/acme/jobs/1", tmp_path)

    assert result.status == "READY"
    assert result.provider == "greenhouse"
    assert result.adapter_confidence == 1.0
    assert result.application_root == "#application_form"
    assert result.blocked_get_origins == ["https://cdn.example.com"]
    assert result.blocked_get_count == 1
    assert result.blocked_write_count == 1
    assert result.blocked_websocket_count == 0
    assert result.network_writes_allowed is False
    assert result.submission_attempted is False
    artifact = Path(result.artifact_path)
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o777 == 0o600
