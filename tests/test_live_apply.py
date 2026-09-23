"""Live fill orchestration, payload building and multipart submission."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import json

import pytest
import httpx
from pypdf import PdfWriter

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.ats import GreenhouseAdapter
from jobsearch_agent.browser import BrowserExecutionResult
from jobsearch_agent.greenhouse import GreenhouseSubmissionExecutor
from jobsearch_agent.models import (
    ApplicationContext,
    ApplicationField,
    ApplicationForm,
    ApplicationPolicy,
    ApplicationState,
    CandidatePreferences,
    Job,
)
from jobsearch_agent.orchestrator import LiveApplicationOrchestrator
from jobsearch_agent.persistence import Database
from jobsearch_agent.profile import load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase
from jobsearch_agent.submission import (
    LiveNetworkPolicy,
    SubmissionBoundaryError,
    SubmissionService,
    build_review_snapshot,
    build_submission_payload,
    submission_destination,
)
from tests.submission_server import SubmissionTestServer


PROFILE = replace(load_profile("profile/career_profile.yaml"), demo=False)
PREFERENCES = CandidatePreferences(relocation=True, work_authorization=["Brazil"])
URL = "https://boards.greenhouse.io/acme/jobs/1"

GREENHOUSE_HTML = """
<form id="application_form">
  <label for="email">Email</label>
  <input id="email" name="job_application[email]" required>
</form>
"""

UNRESOLVED_HTML = """
<form id="application_form">
  <label for="email">Email</label>
  <input id="email" name="job_application[email]" required>
  <label for="custom">Why do you want this role?</label>
  <input id="custom" name="job_application[question_987]" required>
</form>
"""


# --- live orchestration -------------------------------------------------------


class FakeLocator:
    def count(self) -> int:
        return 0


@dataclass
class FakePage:
    html: str
    url: str = URL
    opened: list[str] = field(default_factory=list)

    def content(self) -> str:
        return self.html

    def evaluate(self, *_args, **_kwargs) -> bool:
        return True

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator()


@dataclass
class FakeGuard:
    pending_read_count: int = 0
    blocked_writes: list = field(default_factory=list)


@dataclass
class FakeSession:
    page: FakePage
    network_guard: FakeGuard = field(default_factory=FakeGuard)
    guarded: bool = True

    def open(self, url: str) -> None:
        self.page.opened.append(url)
        self.page.url = url


class RecordingFiller:
    def __init__(self, status: str = "COMPLETED"):
        self.calls: list[dict] = []
        self.status = status

    def fill(self, session, context, plan, bindings, audit_dir=None, *, allow_review=False):
        self.calls.append({"allow_review": allow_review, "actions": len(plan.actions)})
        operations = [{"operation": action.action_type, "field_key": action.field_key} for action in plan.actions]
        return BrowserExecutionResult(context.application_id, operations, True, self.status)


def _context(policy: ApplicationPolicy | None = None) -> ApplicationContext:
    return ApplicationContext(
        application_id="application-live",
        job_id="job-live",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        policy=policy or ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )


def test_live_orchestrator_navigates_fills_and_stops_before_submit():
    page = FakePage(GREENHOUSE_HTML)
    session = FakeSession(page)
    filler = RecordingFiller()
    result = LiveApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=filler
    ).run(session, _context(), URL)

    assert result.status == "FILLED_REVIEW_REQUIRED"
    assert page.opened == [URL]
    assert filler.calls == [{"allow_review": True, "actions": 1}]
    assert result.execution is not None and result.execution.submission_attempted is False
    assert result.form_fingerprint
    assert result.advanced_steps == 0


def test_live_orchestrator_fills_under_review_policy_and_reports_review():
    page = FakePage(GREENHOUSE_HTML)
    filler = RecordingFiller()
    context = _context(ApplicationPolicy(autonomy={"fill_forms": "review", "submit": "manual"}))
    result = LiveApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=filler
    ).run(FakeSession(page), context, URL)

    assert result.status == "FILLED_REVIEW_REQUIRED"
    assert result.readiness is not None and result.readiness.decision == ApplicationState.READY_FOR_REVIEW
    assert filler.calls, "review mode must still fill the page before stopping"


def test_live_orchestrator_never_fills_when_the_safety_gate_blocks():
    page = FakePage(UNRESOLVED_HTML)
    filler = RecordingFiller()
    result = LiveApplicationOrchestrator(
        GreenhouseAdapter(), PROFILE, PREFERENCES, AnswerKnowledgeBase([]), filler=filler
    ).run(FakeSession(page), _context(), URL)

    assert result.status == "NEEDS_ANSWER"
    assert filler.calls == []
    assert result.error


def test_live_orchestrator_refuses_the_demo_profile():
    demo = replace(PROFILE, demo=True)
    result = LiveApplicationOrchestrator(
        GreenhouseAdapter(), demo, PREFERENCES, AnswerKnowledgeBase([]), filler=RecordingFiller()
    ).run(FakeSession(FakePage(GREENHOUSE_HTML)), _context(), URL)

    assert result.status == "DEMO_PROFILE_BLOCKED"


def test_live_orchestrator_attaches_the_generated_resume(tmp_path: Path):
    resume = tmp_path / "resume.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with resume.open("wb") as handle:
        writer.write(handle)
    html = """
    <form id="application_form">
      <label for="email">Email</label>
      <input id="email" name="job_application[email]" required>
      <label for="resume">Resume</label>
      <input id="resume" name="job_application[resume]" type="file" accept="application/pdf" required>
    </form>
    """
    filler = RecordingFiller()
    result = LiveApplicationOrchestrator(
        GreenhouseAdapter(),
        PROFILE,
        PREFERENCES,
        AnswerKnowledgeBase([]),
        filler=filler,
        artifact_root=str(tmp_path),
        default_resume=str(resume),
    ).run(FakeSession(FakePage(html)), _context(), URL)

    assert result.status == "FILLED_REVIEW_REQUIRED"
    resume_fields = [item for item in result.form.fields if item.field_type == "file"]
    assert resume_fields and resume_fields[0].attachment_path == str(resume)
    assert result.form.artifact_root == str(tmp_path)


# --- payload building ---------------------------------------------------------


def _form(tmp_path: Path, *, with_resume: bool = True) -> ApplicationForm:
    fields = [
        ApplicationField(key="job_application[email]", label="Email", required=True, value="candidate@example.test"),
        ApplicationField(key="job_application[first_name]", label="First name", answer=None),
    ]
    if with_resume:
        resume = tmp_path / "resume.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with resume.open("wb") as handle:
            writer.write(handle)
        fields.append(
            ApplicationField(key="job_application[resume]", label="Resume", field_type="file", semantic_type="resume", required=True, attachment_path=str(resume))
        )
    return ApplicationForm(form_id="application", provider="greenhouse", fields=fields, artifact_root=str(tmp_path))


def test_build_submission_payload_keeps_namespaced_keys_and_reads_the_resume(tmp_path: Path):
    payload = build_submission_payload(_form(tmp_path))
    assert payload.fields == {"job_application[email]": "candidate@example.test"}
    assert list(payload.files) == ["job_application[resume]"]
    filename, content, mime = payload.files["job_application[resume]"]
    assert filename == "resume.pdf"
    assert content.startswith(b"%PDF-")
    assert mime == "application/pdf"
    # the empty optional field is reported, not silently invented
    assert "job_application[first_name]" in payload.omitted


def test_build_submission_payload_rejects_a_required_field_without_a_value(tmp_path: Path):
    form = _form(tmp_path)
    form.fields[0].value = ""
    form.fields[0].answer = None
    with pytest.raises(SubmissionBoundaryError, match="required field"):
        build_submission_payload(form)


def test_build_submission_payload_rejects_an_artifact_outside_the_controlled_root(tmp_path: Path):
    form = _form(tmp_path)
    outside = tmp_path.parent / "outside.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with outside.open("wb") as handle:
        writer.write(handle)
    form.fields[2].attachment_path = str(outside)
    with pytest.raises(SubmissionBoundaryError, match="controlled root"):
        build_submission_payload(form)


def test_build_submission_payload_rejects_an_empty_form():
    with pytest.raises(SubmissionBoundaryError, match="empty"):
        build_submission_payload(ApplicationForm(form_id="application", provider="greenhouse"))


# --- real HTTP submission with multipart --------------------------------------


def _ready_application(db: Database, suffix: str) -> str:
    job = Job(id=f"job-{suffix}", source="greenhouse", external_id=suffix, company="Acme", title="Engineer", description="Build software")
    db.save_job(job, f"greenhouse:{suffix}", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "safety_gate_evaluated")
    return application.id


def _authorized_intent(db: Database, application_id: str, destination: str, path: str):
    service = SubmissionService(db)
    intent = service.create_intent(
        application_id=application_id,
        job_id=db.get_application(application_id).job_id,
        provider="greenhouse",
        destination=destination,
        form_fingerprint="form-v1",
        resume_sha256="resume-v1",
        answers_fingerprint="answers-v1",
        expires_in_seconds=300,
        allow_insecure_destination=True,
    )
    service.save_review_snapshot(
        build_review_snapshot(
            application_id=application_id,
            job_id=intent.job_id,
            company="Acme",
            title="Engineer",
            provider="greenhouse",
            destination=destination,
            resume_filename="resume.pdf",
            resume_sha256=intent.resume_sha256,
            form_fingerprint=intent.form_fingerprint,
            answers_fingerprint=intent.answers_fingerprint,
        )
    )
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy(
        provider="greenhouse",
        allowed_origin=destination.split(path)[0],
        allowed_path_pattern=rf"^{path}$",
        allowed_method="POST",
        allowed_stage="SUBMIT",
        application_id=application_id,
        submission_intent_id=intent.id,
    )
    return intent, policy


def test_submission_posts_the_resume_as_a_multipart_file_part(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "multipart")
        destination = server.url("/submit/multipart")
        intent, policy = _authorized_intent(db, application_id, destination, "/submit/multipart")
        payload = build_submission_payload(_form(tmp_path))
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            fields=payload.fields,
            files=payload.files,
        )
        assert result.status == "SUBMITTED"
        assert result.files_sent == 1
        assert result.fields_sent == 1
        assert len(server.requests) == 1
        request = server.requests[0]
        assert request.is_multipart
        assert request.file_parts == ["job_application[resume]"]
        assert b"candidate@example.test" in request.body
        assert db.get_application(application_id).state == ApplicationState.SUBMITTED
        db.close()


def test_submission_without_a_file_part_is_rejected_by_a_real_board(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "nofile")
        destination = server.url("/submit/multipart")
        intent, policy = _authorized_intent(db, application_id, destination, "/submit/multipart")
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            fields={"job_application[email]": "candidate@example.test"},
        )
        assert result.status == "SUBMIT_FAILED"
        assert result.http_status == 422
        assert result.files_sent == 0
        db.close()


def test_html_confirmation_page_is_accepted(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "html")
        destination = server.url("/submit/html")
        intent, policy = _authorized_intent(db, application_id, destination, "/submit/html")
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            fields={"job_application[email]": "candidate@example.test"},
        )
        assert result.status == "SUBMITTED"
        assert db.get_application(application_id).state == ApplicationState.SUBMITTED
        db.close()


def test_ambiguous_html_page_stays_unknown(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "html-unclear")
        destination = server.url("/submit/html-unclear")
        intent, policy = _authorized_intent(db, application_id, destination, "/submit/html-unclear")
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            fields={"job_application[email]": "candidate@example.test"},
        )
        assert result.status == "SUBMIT_UNKNOWN"
        assert result.status != "SUBMITTED"
        db.close()


# --- apply_live wiring --------------------------------------------------------


ROOT = Path(__file__).parents[1]

LIVE_FORM_HTML = """
<form id="application_form">
  <label for="email">Email</label>
  <input id="email" name="job_application[email]" type="email" required>
  <label for="resume">Resume</label>
  <input id="resume" name="job_application[resume]" type="file" accept="application/pdf" required>
</form>
"""


def _live_settings(tmp_path: Path):
    from jobsearch_agent.config import Settings

    return Settings.from_args(
        ROOT,
        db=str(tmp_path / "jobs.db"),
        artifacts=str(tmp_path / "artifacts"),
        profile=str(ROOT / "profile/career_profile.yaml"),
        facts=str(ROOT / "profile/locked_facts.yaml"),
        answers=str(ROOT / "profile/answers.yaml"),
        application_policy=str(ROOT / "profile/application_policy.yaml"),
    )


def _seed_live_job(tmp_path: Path, settings) -> str:
    job = Job(
        id="job-live-glue",
        source="greenhouse",
        external_id="live-glue",
        company="Acme",
        title="Engineer",
        description="Build software",
        url=URL,
    )
    db = Database(settings.resolve(settings.db_path))
    try:
        db.save_job(job, "greenhouse:live-glue", {})
    finally:
        db.close()
    return job.id


def _install_live_stubs(monkeypatch, tmp_path: Path, settings, *, submitted: list):
    """Replace the browser with a stub and the network with an httpx mock."""
    from jobsearch_agent import pipeline
    from jobsearch_agent.application import evaluate_safety_gate
    from jobsearch_agent.orchestrator import LiveApplicationResult

    class FakeSession:
        page = None
        network_guard = None
        guarded = True

        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def open(self, _url: str) -> None:
            pass

    artifact_root = settings.resolve(settings.artifacts_dir)
    # The real prepare() writes the resume before the orchestrator runs.
    resume = artifact_root / "job-live-glue" / "resume.pdf"
    inspected = GreenhouseAdapter().inspect(LIVE_FORM_HTML, URL)
    form = inspected.form
    form.artifact_root = str(resume.parent)
    for field in form.fields:
        if field.field_type == "file":
            field.attachment_path = str(resume)
        elif field.semantic_type == "email":
            field.value = "candidate@example.test"

    class StubOrchestrator:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, session, context, url, audit_dir=None):
            context.form = form
            return LiveApplicationResult(
                "FILLED_REVIEW_REQUIRED",
                provider="greenhouse",
                url=url,
                form=form,
                bindings=inspected.bindings,
                form_fingerprint="fp-live",
                advanced_steps=1,
                readiness=evaluate_safety_gate(context),
            )

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        submitted.append(request)
        return httpx.Response(201, json={"status": "submitted"})

    mock_client = httpx.Client(transport=httpx.MockTransport(_mock_handler), follow_redirects=False)

    class MockedExecutor:
        """The real executor, with its HTTP client pointed at a mock transport."""

        def __init__(self, database, timeout: float = 10.0) -> None:
            self._executor = GreenhouseSubmissionExecutor(database, timeout=timeout, client=mock_client)

        def submit(self, intent_id, **kwargs):
            return self._executor.submit(intent_id, **kwargs)

    monkeypatch.setattr(pipeline, "PlaywrightSessionManager", FakeSession)
    monkeypatch.setattr(pipeline, "LiveApplicationOrchestrator", StubOrchestrator)
    monkeypatch.setattr(pipeline, "GreenhouseSubmissionExecutor", MockedExecutor)
    # prepare() must not call LibreOffice-dependent rendering in this test.
    monkeypatch.setattr(
        pipeline,
        "prepare",
        lambda settings, job_id, language_override=None: {
            "fit": {"blockers": []},
            "resume": {"id": "resume-live"},
            "validation": {"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
            "artifacts": str(resume.parent),
        },
    )
    resume.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with resume.open("wb") as handle:
        writer.write(handle)


def test_apply_live_fills_persists_the_form_and_stops_before_submit(tmp_path: Path, monkeypatch):
    from jobsearch_agent import pipeline

    submitted: list = []
    settings = _live_settings(tmp_path)
    job_id = _seed_live_job(tmp_path, settings)
    _install_live_stubs(monkeypatch, tmp_path, settings, submitted=submitted)

    result = pipeline.apply_live(settings, job_id)

    assert result["status"] == "FILLED_REVIEW_REQUIRED"
    assert result["application_state"] == ApplicationState.REVIEW_REACHED.value
    assert result["advanced_steps"] == 1
    assert result["network_writes_allowed"] is False
    assert submitted == [], "apply without --submit must never post"

    db = Database(settings.resolve(settings.db_path))
    try:
        application = db.get_application(result["application_id"])
        assert application.state == ApplicationState.REVIEW_REACHED
        assert db.get_application_form(application.id) is not None
        live = application.context["validation"]["dry_run"]
        assert live["form_fingerprint"] == "fp-live"
        assert live["source"] == "live"
        assert live["answers_fingerprint"]
        assert application.context["resume_sha256"]
        assert db.list_submission_attempts(application.id) == []
    finally:
        db.close()


def test_apply_live_with_submit_authorizes_and_posts_once_with_the_resume(tmp_path: Path, monkeypatch):
    from jobsearch_agent import pipeline

    submitted: list = []
    settings = _live_settings(tmp_path)
    job_id = _seed_live_job(tmp_path, settings)
    _install_live_stubs(monkeypatch, tmp_path, settings, submitted=submitted)

    result = pipeline.apply_live(settings, job_id, submit=True)

    assert result["status"] == "FILLED_REVIEW_REQUIRED"
    assert result["submission"]["status"] == "SUBMITTED"
    assert result["submission"]["files_sent"] == 1
    assert result["application_state"] == ApplicationState.SUBMITTED.value
    assert len(submitted) == 1
    request = submitted[0]
    assert "multipart/form-data" in request.headers["content-type"]
    assert b'name="job_application[resume]"' in request.content
    assert b'filename="resume.pdf"' in request.content
    assert b"candidate@example.test" in request.content

    db = Database(settings.resolve(settings.db_path))
    try:
        application = db.get_application(result["application_id"])
        assert application.state == ApplicationState.SUBMITTED
        snapshot = db.get_review_snapshot(application.id)
        assert snapshot is not None
        assert snapshot.form_fingerprint == "fp-live"
        # O POST vai para o submitPath publicado pelo board, nao para a pagina
        # do formulario (job-boards), que a LiveNetworkPolicy recusaria.
        assert snapshot.destination == submission_destination("greenhouse", "Acme", "live-glue")
        assert db.get_submission_intent(result["submission"]["intent_id"]).status == "SUBMITTED"
    finally:
        db.close()



def test_cli_exposes_apply_and_reports_a_missing_job(tmp_path: Path, capsys):
    from jobsearch_agent.cli import main

    with pytest.raises(SystemExit):
        main(["apply", "--help"])
    help_text = capsys.readouterr().out
    assert "--submit" in help_text
    assert "--no-advance" in help_text

    result = main(
        [
            "apply",
            "missing-job",
            "--db",
            str(tmp_path / "jobs.db"),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert "not found" in output["error"]
