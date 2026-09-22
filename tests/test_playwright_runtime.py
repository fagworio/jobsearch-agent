from pathlib import Path

import pytest

pytest.importorskip("playwright")
from pypdf import PdfWriter
from playwright.sync_api import sync_playwright

from jobsearch_agent.browser import PlaywrightFormFiller
from jobsearch_agent.execution import build_execution_plan
from jobsearch_agent.inspector import ATSInspector
from jobsearch_agent.models import ApplicationContext, ApplicationPolicy


def test_local_chromium_dry_run_inspects_fills_uploads_and_screenshots(tmp_path: Path):
    artifact = tmp_path / "resume.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with artifact.open("wb") as handle:
        writer.write(handle)

    html = """
    <form id="application">
      <label for="name">Name</label><input id="name" name="name" required>
      <label for="resume">Resume</label><input id="resume" name="resume" type="file" accept="application/pdf" required>
    </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_content(html)
        inspected = ATSInspector().inspect_page(page, "https://boards.greenhouse.io/example", form_selector="#application")
        inspected.form.artifact_root = str(tmp_path)
        for field in inspected.form.fields:
            if field.key == "name":
                field.value = "Candidate"
            if field.key == "resume":
                field.attachment_path = str(artifact)
        application_context = ApplicationContext(
            application_id="application-browser-fixture",
            job_id="job-browser-fixture",
            fit={"blockers": []},
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            form=inspected.form,
            policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
        )
        plan = build_execution_plan(application_context, inspected.bindings)
        result = PlaywrightFormFiller().fill(page, application_context, plan, inspected.bindings)
        screenshot = tmp_path / "dry-run.png"
        page.screenshot(path=str(screenshot))
        assert page.locator("#name").input_value() == "Candidate"
        assert page.locator("#resume").evaluate("element => element.files.length") == 1
        assert result.stopped_before_submit is True
        assert screenshot.is_file()
        assert not hasattr(PlaywrightFormFiller(), "submit")
        context.close()
        browser.close()
