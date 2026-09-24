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


# --- browser falso para submissao (usa o NetworkWriteGuard real) --------------


class _FakeResponse:
    def __init__(self, url: str, status: int) -> None:
        self.url = url
        self.status = status


class _RecordingRequest:
    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url
        self.resource_type = "fetch"


class _FakeButton:
    def __init__(self, on_click) -> None:
        self._on_click = on_click

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self._on_click()


class _FakeLocatorList:
    def __init__(self, items) -> None:
        self._items = list(items)

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int):
        return self._items[index]


class _FakeIframe:
    """Iframe minimo: o adapter le src, visibilidade e caixa — nada mais."""

    def __init__(self, src: str, *, width: int = 300, height: int = 400, visible: bool = True) -> None:
        self._src = src
        self._width = width
        self._height = height
        self._visible = visible

    def get_attribute(self, name: str) -> str:
        return self._src if name == "src" else ""

    def is_visible(self) -> bool:
        return self._visible

    def bounding_box(self) -> dict:
        return {"width": self._width, "height": self._height}


class FakeSubmitPage:
    """Pagina minima capaz de disparar o POST pelo guard real."""

    def __init__(
        self,
        guard,
        destination: str,
        *,
        status: int = 201,
        confirmed: bool = True,
        captcha: bool = False,
        challenge_path: str = "/recaptcha/enterprise/bframe",
    ) -> None:
        from jobsearch_agent.browser import _origin_of

        self._guard = guard
        self._destination = destination
        self._status = status
        self._confirmed = confirmed
        self._captcha = captcha
        self._challenge_path = challenge_path
        self._listeners: list[tuple[str, object]] = []
        self._clicks = 0
        self.url = _origin_of(destination) + "/canonical/jobs/5150422"

    def content(self) -> str:
        """DOM servido ao challenge-guard.

        Com desafio, o container aparece — e com `challenge_path`, o frame vem
        como `bframe`, que e o popup interativo, nao o selo.
        """
        if not self._captcha:
            return '<form id="application_form"><input name="email"></form>'
        return '<div class="g-recaptcha" data-sitekey="REDACTED"></div>'

    def query_selector_all(self, selector: str) -> list:
        if selector != "iframe" or not self._captcha:
            return []
        return [_FakeIframe(f"https://www.recaptcha.net{self._challenge_path}")]

    def on(self, event: str, handler) -> None:
        self._listeners.append((event, handler))

    def remove_listener(self, event: str, handler) -> None:
        self._listeners = [item for item in self._listeners if item != (event, handler)]

    def get_by_role(self, role: str, name: str = "", exact: bool = False):
        if role == "button" and name == "Submit application":
            return _FakeLocatorList([_FakeButton(self._submit)])
        return _FakeLocatorList([])

    def locator(self, selector: str):
        return _FakeLocatorList([])

    @property
    def frames(self) -> list:
        if self._captcha:
            return [type("_Frame", (), {"url": "https://www.recaptcha.net/recaptcha/enterprise/challenge"})()]
        return []

    def inner_text(self, _selector: str) -> str:
        return "Thank you for applying" if self._confirmed else "Form"

    def wait_for_timeout(self, _ms: int) -> None:
        return

    def _submit(self) -> None:
        self._clicks += 1
        if self._captcha:
            return  # desafio apresentado: nenhuma escrita sai do browser
        if not self._guard.inspect(_RecordingRequest("POST", self._destination)):
            return  # bloqueado pelo guard: nenhuma resposta e observada
        for event, handler in self._listeners:
            if event == "response":
                handler(_FakeResponse(self._destination, self._status))
        if self._confirmed:
            self.url = self._destination.rsplit("/jobs/", 1)[0] + "/jobs/5150422/confirmation"


class FakeGuardedSession:
    def __init__(self, guard, page) -> None:
        self.network_guard = guard
        self.page = page
        self.guarded = True

    def arm_authorized_write(self, permit) -> None:
        self.network_guard.arm_write(permit)

    def arm_writes(self, permits) -> None:
        self.network_guard.arm_writes(list(permits))

    def disarm_authorized_write(self) -> None:
        self.network_guard.disarm_write()

    def arm_challenge_runtime(self, permits) -> None:
        self.network_guard.arm_challenge_runtime(list(permits))

    def disarm_challenge_runtime(self) -> None:
        self.network_guard.disarm_challenge_runtime()


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

    from jobsearch_agent.browser import NetworkWriteGuard

    destination = submission_destination("greenhouse", "Acme", "live-glue")
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    submit_page = FakeSubmitPage(guard, destination)

    class FakeSession(FakeGuardedSession):
        def __init__(self, **_kwargs) -> None:
            super().__init__(guard, submit_page)

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def open(self, _url: str) -> None:
            pass

    _install_live_stubs.submit_page = submit_page  # type: ignore[attr-defined]
    _install_live_stubs.guard = guard  # type: ignore[attr-defined]

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
    assert result["network_guard_active"] is True
    assert result["write_policy"] == "deny_all"
    assert result["upload_writes_used"] == 0
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
    assert result["submission"]["transport"] == "browser"
    assert result["application_state"] == ApplicationState.SUBMITTED.value
    assert result["submission"]["http_status"] == 201
    # exatamente uma escrita foi autorizada e consumida pelo guard
    guard = _install_live_stubs.guard  # type: ignore[attr-defined]
    assert guard.authorized_writes_used == 1
    assert guard.authorized_writes_remaining == 0
    assert guard.blocked_writes == []

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


def test_submission_without_the_handshake_is_rejected_by_the_endpoint(tmp_path: Path):
    """Sem o token anti-CSRF o board recusa; e o handshake que o obtem."""
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "csrf-missing")
        destination = server.url("/submit/needs-token")
        intent, policy = _authorized_intent(db, application_id, destination, "/submit/needs-token")
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            fields={"job_application[email]": "candidate@example.test"},
        )
        assert result.status == "SUBMIT_FAILED"
        assert result.http_status == 400
        db.close()


def test_submission_handshake_collects_the_csrf_token_and_succeeds(tmp_path: Path):
    with SubmissionTestServer() as server:
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "csrf-ok")
        destination = server.url("/submit/needs-token")
        intent, policy = _authorized_intent(db, application_id, destination, "/submit/needs-token")
        result = GreenhouseSubmissionExecutor(db).submit(
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
            fields={"job_application[email]": "candidate@example.test"},
            session_url=server.url("/job-with-token"),
        )
        assert result.status == "SUBMITTED"
        assert result.http_status == 201
        # o token foi enviado junto com os campos do formulario
        body = server.requests[-1].body
        assert b"tok-abc123" in body
        db.close()


def test_csrf_field_extraction_covers_the_common_markups():
    from jobsearch_agent.greenhouse import csrf_field

    assert csrf_field('<meta name="csrf-token" content="abc123">') == ("csrf-token", "abc123")
    assert csrf_field('<meta content="xyz" name="csrf-token">') == ("csrf-token", "xyz")
    assert csrf_field('<input name="authenticity_token" value="tok9">') == ("authenticity_token", "tok9")
    assert csrf_field('<input value="tok9" name="authenticity_token">') == ("authenticity_token", "tok9")
    assert csrf_field('{"csrfToken":"jwt-token"}') == ("authenticity_token", "jwt-token")
    assert csrf_field("&lt;meta name=&quot;csrf-token&quot; content=&quot;esc&amp;aped&quot;&gt;") == ("csrf-token", "esc&aped")
    assert csrf_field("<html>no token here</html>") == ("", "")


# --- recovery explicito de SUBMIT_FAILED --------------------------------------


def _attempt(db: Database, application_id: str, path: str, suffix: str, *, timeout: float = 10.0):
    destination = db.get_application(application_id)
    intent, policy = _authorized_intent(db, application_id, _SERVER_URL[0] + path, path)
    executor = GreenhouseSubmissionExecutor(db, timeout=timeout)
    return executor.submit(
        intent.id,
        current_form_fingerprint="form-v1",
        current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1",
        policy=policy,
        fields={"job_application[email]": "candidate@example.test"},
        session_url=_SERVER_URL[0] + "/job-with-token",
    )


_SERVER_URL: list[str] = []


def test_retry_submit_reopens_an_application_after_a_definitive_failure(tmp_path: Path):
    from jobsearch_agent.application import ApplicationService

    with SubmissionTestServer() as server:
        _SERVER_URL[:] = [server.origin]
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "retry-failed")
        result = _attempt(db, application_id, "/submit/error", "retry-failed")
        assert result.status == "SUBMIT_FAILED"
        assert db.get_application(application_id).state == ApplicationState.SUBMIT_FAILED

        reopened = ApplicationService(db).retry_submit(application_id)
        assert reopened.state == ApplicationState.REVIEW_REACHED
        events = [event.event for event in db.list_application_events(application_id)]
        assert "submit_retry_authorized" in events
        db.close()


def test_retry_submit_refuses_an_unknown_outcome(tmp_path: Path):
    """SUBMIT_UNKNOWN nunca volta: nao se sabe se a candidatura foi aceita."""
    from jobsearch_agent.application import ApplicationDomainError, ApplicationService

    with SubmissionTestServer() as server:
        _SERVER_URL[:] = [server.origin]
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "retry-unknown")
        result = _attempt(db, application_id, "/submit/timeout", "retry-unknown", timeout=0.05)
        assert result.status == "SUBMIT_UNKNOWN"

        with pytest.raises(ApplicationDomainError, match="requires SUBMIT_FAILED"):
            ApplicationService(db).retry_submit(application_id)
        assert db.get_application(application_id).state == ApplicationState.SUBMIT_UNKNOWN
        db.close()


def test_retry_submit_refuses_when_the_attempt_outcome_is_unknown(tmp_path: Path):
    """Segunda barreira: mesmo com estado inconsistente, tentativa UNKNOWN barra."""
    from jobsearch_agent.application import ApplicationDomainError, ApplicationService

    with SubmissionTestServer() as server:
        _SERVER_URL[:] = [server.origin]
        db = Database(tmp_path / "submission.db")
        application_id = _ready_application(db, "retry-inconsistent")
        result = _attempt(db, application_id, "/submit/timeout", "retry-inconsistent", timeout=0.05)
        assert result.status == "SUBMIT_UNKNOWN"

        # Forca o estado para SUBMIT_FAILED e mantem a tentativa UNKNOWN.
        application = db.get_application(application_id)
        application.state = ApplicationState.SUBMIT_FAILED
        db.save_application(application)

        with pytest.raises(ApplicationDomainError, match="must never be resent"):
            ApplicationService(db).retry_submit(application_id)
        db.close()


def test_retry_submit_refuses_when_the_state_is_not_a_failed_submission(tmp_path: Path):
    from jobsearch_agent.application import ApplicationDomainError, ApplicationService

    db = Database(tmp_path / "submission.db")
    application_id = _ready_application(db, "retry-wrong-state")
    with pytest.raises(ApplicationDomainError, match="requires SUBMIT_FAILED"):
        ApplicationService(db).retry_submit(application_id)
    db.close()


# --- submissao pelo browser ---------------------------------------------------

BROWSER_DEST = "https://boards.greenhouse.io/acme/jobs/4242"
BROWSER_PATH = "/acme/jobs/4242"


def _browser_submit(db: Database, suffix: str, *, status: int = 201, confirmed: bool = True, captcha: bool = False):
    from jobsearch_agent.browser import NetworkWriteGuard
    from jobsearch_agent.submission_browser import GreenhouseBrowserSubmitter

    application_id = _ready_application(db, suffix)
    intent, policy = _authorized_intent(db, application_id, BROWSER_DEST, BROWSER_PATH)
    guard = NetworkWriteGuard({"boards.greenhouse.io"})
    page = FakeSubmitPage(guard, BROWSER_DEST, status=status, confirmed=confirmed, captcha=captcha)
    session = FakeGuardedSession(guard, page)
    outcome = GreenhouseBrowserSubmitter(db, timeout_seconds=1.0).submit(
        session,
        intent.id,
        current_form_fingerprint="form-v1",
        current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1",
        policy=policy,
    )
    return outcome, guard, application_id


def test_browser_submission_confirms_and_records_submitted(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    outcome, guard, application_id = _browser_submit(db, "br-confirm")
    assert outcome.status == "SUBMITTED"
    assert outcome.http_status == 201
    assert guard.authorized_writes_used == 1
    assert guard.authorized_writes_remaining == 0
    assert db.get_application(application_id).state == ApplicationState.SUBMITTED
    db.close()


def test_browser_submission_without_confirmation_stays_unknown(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    outcome, guard, application_id = _browser_submit(db, "br-unknown", confirmed=False)
    assert outcome.status == "SUBMIT_UNKNOWN"
    assert guard.authorized_writes_used == 1
    assert db.get_application(application_id).state == ApplicationState.SUBMIT_UNKNOWN
    db.close()


def test_browser_submission_rejection_is_a_definitive_failure(tmp_path: Path):
    db = Database(tmp_path / "submission.db")
    outcome, _, application_id = _browser_submit(db, "br-rejected", status=422, confirmed=False)
    assert outcome.status == "SUBMIT_FAILED"
    assert outcome.http_status == 422
    assert db.get_application(application_id).state == ApplicationState.SUBMIT_FAILED
    db.close()


def test_browser_submission_never_allows_a_second_write(tmp_path: Path):
    """O guard cobre um POST; um segundo envio do formulario e bloqueado."""
    db = Database(tmp_path / "submission.db")
    outcome, guard, application_id = _browser_submit(db, "br-double")
    assert outcome.status == "SUBMITTED"
    assert guard.authorized_writes_remaining == 0
    # O proprio formulario dispara de novo: tem de ser bloqueado.
    guard.inspect(type("R", (), {"method": "POST", "url": BROWSER_DEST, "resource_type": "fetch"})())
    assert guard.blocked_writes, "segunda escrita deveria ser bloqueada"
    db.close()


def test_browser_submission_stops_on_captcha_without_sending(tmp_path: Path):
    from jobsearch_agent.submission_browser import GreenhouseBrowserSubmitter

    db = Database(tmp_path / "submission.db")
    outcome, guard, application_id = _browser_submit(db, "br-captcha", captcha=True)
    assert outcome.status == "NEEDS_CAPTCHA"
    # Nada saiu do browser e a permissao segue intacta.
    assert guard.authorized_writes_used == 0
    # A permissao e sempre desarmada ao fim, mesmo sem ter sido consumida.
    assert guard.authorized_write is None
    assert "CAPTCHA" in outcome.error
    # Falha definitiva (o guard prova que nenhuma escrita ocorreu), portanto
    # retomavel por `application retry-submit`.
    assert db.get_application(application_id).state == ApplicationState.SUBMIT_FAILED
    attempts = db.list_submission_attempts(application_id)
    assert "captcha_no_write" in str(attempts[-1])
    db.close()


# --- ACL do challenge-guard ---------------------------------------------------


class _AclPage:
    """Pagina minima para a ACL: entrega DOM e frames, nada mais."""

    url = "https://example.invalid/"

    def __init__(self, *, html: str = "", iframes: list | None = None) -> None:
        self._html = html
        self._iframes = list(iframes or [])

    def on(self, *_args) -> None:
        return None

    def remove_listener(self, *_args) -> None:
        return None

    def content(self) -> str:
        return self._html

    def query_selector_all(self, selector: str) -> list:
        return list(self._iframes) if selector == "iframe" else []


def _acl_outcome(*, page=None, page_errors=(), http_status=None, writes=0, confirmed=False):
    """Veredito anti-bot pelo caminho de producao: ACL -> challenge-guard."""
    from jobsearch_agent.challenges import JobsearchChallengeAdapter

    adapter = JobsearchChallengeAdapter()
    if page is not None:
        adapter.attach(page)
    return adapter.observe(
        step="post_submit",
        http_status=http_status,
        page_errors=page_errors,
        browser_write_sent=bool(writes),
        submission_confirmed=confirmed,
    )


def _recaptcha_challenge_page():
    return _AclPage(
        html='<div class="g-recaptcha" data-sitekey="REDACTED"></div>',
        iframes=[_FakeIframe("https://www.recaptcha.net/recaptcha/enterprise/bframe")],
    )


def test_classification_of_the_browser_outcome():
    from jobsearch_agent.submission_browser import GreenhouseBrowserSubmitter

    classifier = GreenhouseBrowserSubmitter.__dict__["_classify"]
    submitter = GreenhouseBrowserSubmitter.__new__(GreenhouseBrowserSubmitter)

    def classify(observed, writes):
        return classifier(submitter, observed, writes, _acl_outcome(http_status=observed.get("http_status"), writes=writes, confirmed=bool(observed.get("confirmation_reached"))))

    verification, status = classify({"http_status": 201, "confirmation_reached": True}, 1)
    assert status == "SUBMITTED" and verification.status == "confirmed"

    verification, status = classify({"http_status": 201, "confirmation_reached": False}, 1)
    assert status == "SUBMIT_UNKNOWN" and verification.status == "unknown"

    verification, status = classify({"http_status": 500, "confirmation_reached": False}, 1)
    assert status == "SUBMIT_FAILED" and verification.status == "failed"

    verification, status = classify({"http_status": None, "confirmation_reached": False}, 0)
    assert status == "SUBMIT_FAILED"


def test_a_pre_submit_challenge_is_needs_captcha_not_a_server_rejection():
    """Challenge bloqueando antes de qualquer escrita: nada foi recusado."""
    from jobsearch_agent.submission_browser import GreenhouseBrowserSubmitter

    classifier = GreenhouseBrowserSubmitter.__dict__["_classify"]
    submitter = GreenhouseBrowserSubmitter.__new__(GreenhouseBrowserSubmitter)
    outcome = _acl_outcome(page=_recaptcha_challenge_page(), writes=0)
    assert outcome.decision == "needs_human"
    verification, status = classifier(submitter, {}, 0, outcome)
    assert status == "NEEDS_CAPTCHA" and verification is None


def test_classification_treats_a_server_captcha_demand_as_human_handoff():
    """reCAPTCHA Enterprise e invisivel: o 428 com a mensagem e o sinal.

    Com uma escrita entregue, o provedor recusou uma submissao real. Repetir nao
    muda o veredito, e disfarcar os sinais de automacao esta fora de escopo.
    """
    from jobsearch_agent.submission_browser import GreenhouseBrowserSubmitter

    submitter = GreenhouseBrowserSubmitter.__new__(GreenhouseBrowserSubmitter)
    classifier = GreenhouseBrowserSubmitter.__dict__["_classify"]
    message = "Please complete the reCAPTCHA and resubmit your application."

    outcome = _acl_outcome(page_errors=[message], http_status=428, writes=1, confirmed=False)
    assert outcome.decision == "provider_rejected"
    verification, status = classifier(submitter, {"http_status": 428}, 1, outcome)
    assert status == "NEEDS_HUMAN_CAPTCHA"
    assert verification.status == "challenged"
    assert verification.evidence["reason_token"] == "captcha_verification_failed"
    assert verification.evidence["submit_write"] is True
    assert verification.evidence["confirmed_submission"] is False
    assert verification.evidence["status_code"] == 428

    # A mensagem real do outro provider, com 400: mesmo desfecho.
    other = _acl_outcome(
        page_errors=["There was an error verifying your application."], http_status=400, writes=1
    )
    assert other.decision == "provider_rejected"
    assert classifier(submitter, {"http_status": 400}, 1, other)[1] == "NEEDS_HUMAN_CAPTCHA"

    # Sem escrita entregue nao ha submissao recusada: e o desafio a resolver.
    unsent = _acl_outcome(page=_recaptcha_challenge_page(), writes=0)
    assert classifier(submitter, {}, 0, unsent)[1] == "NEEDS_CAPTCHA"

    # Uma rejeicao comum continua sendo falha definitiva, nao CAPTCHA.
    plain = _acl_outcome(page_errors=["Resume/CV is required."], http_status=422, writes=1)
    assert plain.decision != "provider_rejected"
    verification, status = classifier(submitter, {"http_status": 422}, 1, plain)
    assert status == "SUBMIT_FAILED"


def test_classification_keeps_a_confirmed_submission_after_a_solved_captcha():
    """Uma candidatura aceita nao volta a 'precisa humano' por um iframe no DOM.

    A precedencia final de SUBMITTED e da Submission Boundary, nao do guard: o
    widget pode continuar no DOM depois de resolvido.
    """
    from jobsearch_agent.submission_browser import GreenhouseBrowserSubmitter

    submitter = GreenhouseBrowserSubmitter.__new__(GreenhouseBrowserSubmitter)
    classifier = GreenhouseBrowserSubmitter.__dict__["_classify"]

    solved = _acl_outcome(
        page=_recaptcha_challenge_page(), http_status=200, writes=1, confirmed=True
    )
    assert solved.decision == "none"
    assert solved.reason_token == "submission_confirmed_overrides_challenge"
    verification, status = classifier(submitter, {"http_status": 200, "confirmation_reached": True}, 1, solved)
    assert status == "SUBMITTED" and verification.status == "confirmed"

    # Sem escrita, o desafio visivel segue sendo o desfecho a reportar.
    unsolved = _acl_outcome(page=_recaptcha_challenge_page(), writes=0)
    assert classifier(submitter, {}, 0, unsolved)[1] == "NEEDS_CAPTCHA"


def test_a_redirect_to_the_confirmation_page_is_a_confirmed_submission():
    """303 para `/confirmation` é o desfecho de sucesso, não incerteza.

    A maioria dos ATS responde ao POST com redirect para a página de
    confirmação. Aceitar apenas 2xx classificava um envio ACEITO como
    `SUBMIT_UNKNOWN` — e um `SUBMIT_UNKNOWN` nunca é reenviado, então a
    candidatura ficava eternamente ambígua mesmo tendo chegado.
    """
    from jobsearch_agent.submission_browser import BrowserSubmitter

    submitter = BrowserSubmitter.__new__(BrowserSubmitter)
    classifier = BrowserSubmitter.__dict__["_classify"]

    verification, status = classifier(submitter, {"http_status": 303, "confirmation_reached": True}, 1, None)
    assert status == "SUBMITTED"
    assert verification.status == "confirmed"
    assert verification.evidence["status_code"] == 303

    # 2xx com confirmação continua sendo confirmação.
    assert classifier(submitter, {"http_status": 200, "confirmation_reached": True}, 1, None)[1] == "SUBMITTED"

    # Sem confirmação observada, um 303 NÃO é sucesso: é escrita sem desfecho.
    assert classifier(submitter, {"http_status": 303, "confirmation_reached": False}, 1, None)[1] == "SUBMIT_UNKNOWN"

    # Erro do servidor com confirmação ambígua não vira sucesso.
    assert classifier(submitter, {"http_status": 500, "confirmation_reached": True}, 1, None)[1] == "SUBMIT_FAILED"
