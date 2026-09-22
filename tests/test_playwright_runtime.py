from pathlib import Path

import pytest

pytest.importorskip("playwright")
from pypdf import PdfWriter
from jobsearch_agent.browser import PlaywrightFormFiller, PlaywrightSessionManager
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
    manager = PlaywrightSessionManager(allowed_hosts={"example.com"})
    manager.start()
    try:
        page = manager.page
        page.set_content(html)
        inspected = ATSInspector().inspect_page(page, form_selector="#application")
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
        audit_dir = tmp_path / "browser"
        result = PlaywrightFormFiller().fill(page, application_context, plan, inspected.bindings, audit_dir=audit_dir)
        post_result = page.evaluate("""async () => {
            try { await fetch('https://example.com/write', {method: 'POST', body: 'blocked'}); return 'sent'; }
            catch (error) { return 'blocked'; }
        }""")
        assert page.locator("#name").input_value() == "Candidate"
        assert page.locator("#resume").evaluate("element => element.files.length") == 1
        assert result.stopped_before_submit is True
        assert post_result == "blocked"
        assert (audit_dir / "screenshot-before.png").is_file()
        assert (audit_dir / "screenshot-after.png").is_file()
        assert (audit_dir / "dry-run-report.json").is_file()
        assert not hasattr(PlaywrightFormFiller(), "submit")
        assert manager.network_guard is not None
        assert manager.context is not None
        assert any(event.method == "POST" and not event.allowed for event in manager.network_guard.events)
    finally:
        manager.close()
