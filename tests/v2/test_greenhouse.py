from __future__ import annotations

import pytest

from job_agent_v2.ats import ATSInspectionError, GreenhouseAdapter


SNAPSHOT = {
    "provider": "greenhouse",
    "page_type": "application",
    "url": "https://job-boards.greenhouse.io/example/jobs/1",
    "title": "Job Application",
    "ready": True,
    "fields": [
        {"id": "first_name", "type": "text", "label": "First Name", "required": True, "options": [], "value": ""},
        {"id": "resume", "type": "file", "label": "Resume/CV", "required": True, "options": [], "value": ""},
        {"id": "question_work_auth", "type": "radio", "label": "Are you authorized?", "required": True, "options": ["Yes", "No"], "value": ""},
        {"id": "gender", "type": "checkbox", "label": "Gender", "required": False, "options": ["Woman", "Man"], "value": ""},
    ],
}


def test_greenhouse_adapter_preserves_every_neutral_required_field():
    form = GreenhouseAdapter().to_form(SNAPSHOT)
    assert len(form.fields) == 4
    assert [field.prompt for field in form.required()] == ["First Name", "Resume/CV", "Are you authorized?"]
    assert form.fields[2].options == ("Yes", "No")


def test_greenhouse_adapter_rejects_non_application_snapshots():
    snapshot = dict(SNAPSHOT, page_type="job")
    with pytest.raises(ATSInspectionError, match="page type"):
        GreenhouseAdapter().inspect(snapshot)


def test_greenhouse_adapter_does_not_invent_missing_fields():
    snapshot = dict(SNAPSHOT, fields=[])
    with pytest.raises(ATSInspectionError, match="no fields"):
        GreenhouseAdapter().inspect(snapshot)


def test_greenhouse_adapter_accepts_semantic_checkbox_group_labels():
    snapshot = dict(SNAPSHOT, fields=[{
        "id": "question_1[]",
        "type": "checkbox_group",
        "label": "Which time zones can you work?",
        "required": True,
        "options": ["Eastern Time (ET)", "Central Time (CT)", "None of the above"],
        "value": "",
    }])
    form = GreenhouseAdapter().to_form(snapshot)
    assert form.fields[0].kind == "checkbox_group"
    assert form.fields[0].options == ("Eastern Time (ET)", "Central Time (CT)", "None of the above")
