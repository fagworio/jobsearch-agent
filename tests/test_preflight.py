from pathlib import Path

from jobsearch_agent.browser import BrowserSessionError, DOMStabilityGuard, NetworkRequestEvent
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
    assert result.field_keys
    assert result.field_count == len(result.field_keys)
    assert result.blocked_get_origins == ["https://cdn.example.com"]
    assert result.blocked_get_count == 1
    assert result.blocked_write_count == 1
    assert result.blocked_websocket_count == 0
    assert result.network_writes_allowed is False
    assert result.submission_attempted is False
    artifact = Path(result.artifact_path)
    assert artifact.is_file()
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert '"artifact_path":' in artifact.read_text(encoding="utf-8")


def test_preflight_preserves_settling_and_adapter_warnings(tmp_path, monkeypatch):
    original_content = FakePage.content

    def content_with_unknown_required_field(self):
        return original_content(self).replace(
            "</form>",
            '<label for="q">Custom question</label><input id="q" name="job_application[question_custom]" required></form>',
        )

    monkeypatch.setattr(FakePage, "content", content_with_unknown_required_field)
    monkeypatch.setattr("jobsearch_agent.preflight.PlaywrightSessionManager", FakeSession)
    monkeypatch.setattr(DOMStabilityGuard, "wait", lambda *_args, **_kwargs: (_ for _ in ()).throw(BrowserSessionError("DOM_UNSTABLE fixture")))
    result = run_preflight("https://boards.greenhouse.io/acme/jobs/1", tmp_path)
    assert result.status == "DOM_UNSTABLE"
    assert "DOM_UNSTABLE fixture" in result.warnings
    assert "required field has no high-confidence semantic mapping" in result.warnings
