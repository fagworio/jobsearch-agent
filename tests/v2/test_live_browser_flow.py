from __future__ import annotations

from pathlib import Path
from typing import Any

from job_agent_v2.apply import apply
from job_agent_v2.fill import fill
from job_agent_v2.models import State
from job_agent_v2.submit import SubmitState, submit


def _snapshot() -> dict[str, Any]:
    return {
        "provider": "greenhouse",
        "page_type": "application",
        "url": "https://job-boards.greenhouse.io/example/jobs/1",
        "title": "Example",
        "ready": True,
        "fields": [
            {
                "id": "first_name",
                "type": "text",
                "label": "First Name",
                "required": True,
                "options": [],
                "value": "",
                "checked": False,
            },
            {
                "id": "resume",
                "type": "file",
                "label": "Resume",
                "required": False,
                "options": [],
                "value": "",
                "checked": False,
            },
        ],
    }


class FakeBrowser:
    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.fill_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.submit_calls: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeBrowser":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def inspect_form(self) -> dict[str, Any]:
        return self.snapshot

    def upload_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.upload_calls.append(payload)
        self._field("resume")["value"] = str(payload["filename"])
        return self.snapshot

    def fill_form(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.fill_calls.append(payload)
        self._field(str(payload["field_id"]))["value"] = str(payload.get("value") or "")
        return self.snapshot

    def read_form(self) -> dict[str, Any]:
        return self.snapshot

    def challenge_state(self) -> dict[str, Any]:
        return {"state": "CLEAR", "signals": []}

    def request_submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submit_calls.append(payload)
        return {"submitted": True}

    def submit_result(self) -> dict[str, Any]:
        return {
            "state": "SUBMITTED",
            "primary": {
                "url": self.snapshot["url"],
                "confirmation_component": True,
                "error_component": False,
                "detail": "visible confirmation evidence",
            },
            "secondary": [],
        }

    def _field(self, field_id: str) -> dict[str, Any]:
        return next(field for field in self.snapshot["fields"] if field["id"] == field_id)


def test_apply_default_uses_extension_snapshot(monkeypatch):
    browser = FakeBrowser()
    monkeypatch.setattr("job_agent_v2.apply.NativeMessagingClient", lambda: browser)

    result = apply(
        "https://job-boards.greenhouse.io/example/jobs/1",
        profile={"First Name": "João"},
    )

    assert result.state is State.READY
    assert result.answers == {"first_name": "João"}


def test_fill_live_path_uploads_and_reads_back(monkeypatch, tmp_path: Path):
    browser = FakeBrowser()
    monkeypatch.setattr("job_agent_v2.fill.NativeMessagingClient", lambda: browser)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nfake")

    result = fill(
        "https://job-boards.greenhouse.io/example/jobs/1",
        profile={"First Name": "João"},
        resume=str(resume),
    )

    assert result.state is State.FILLED
    assert result.resume_attached is True
    assert len(browser.upload_calls) == 1
    assert browser.fill_calls == [{"field_id": "first_name", "action": "set", "value": "João"}]


def test_submit_live_path_dispatches_one_authorized_action(monkeypatch, tmp_path: Path):
    browser = FakeBrowser()
    monkeypatch.setattr("job_agent_v2.submit.NativeMessagingClient", lambda: browser)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nfake")

    result = submit(
        "https://job-boards.greenhouse.io/example/jobs/1",
        profile={"First Name": "João"},
        resume=str(resume),
        store=str(tmp_path / "markers"),
    )

    assert result.state is SubmitState.SUBMITTED
    assert len(browser.submit_calls) == 1
    assert result.submission_writes == 1
