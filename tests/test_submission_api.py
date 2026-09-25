"""O canal de API: guard PROPRIO, contabilidade de intent/tentativa, e mocks.

Nada aqui toca a rede. O transporte e um fake, e o unico destino exercitado e um
endpoint que nao existe. Isso e deliberado e e o estado atual do ticket: sem
credencial real de ATS o endpoint autenticado NAO pode ser medido, e um teste que
fingisse medir seria pior do que nenhum. O que se prova aqui e o que o codigo
pode provar sozinho:

    o canal degrada para BROWSER sem credencial declarada
    a escrita sai UMA vez, com a credencial no header, e nunca e repetida
    redirect nao e seguido (seria uma segunda escrita, para outra origem)
    2xx confirma; 4xx recusa; 5xx e falha de transporte sao `unknown`
    o segredo nao chega ao banco, ao evento, a evidencia nem ao `repr`
    o canal de API nao importa o browser, o relay nem o motor de resolucao
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.coordinator import SubmissionResult
from jobsearch_agent.models import ApplicationField, ApplicationForm, ApplicationState, Job, ValidationResult
from jobsearch_agent.persistence import Database
from jobsearch_agent.submission import API_REASON_TOKENS, SAFE_REASON_TOKENS, SubmissionBoundaryError
from jobsearch_agent.submission_api import (
    ApiCredential,
    ApiRequest,
    ApiResponse,
    ApiSubmissionError,
    ApiSubmitter,
    ApiWriteGuard,
    ApiWritePolicy,
    SubmissionRouter,
)
from jobsearch_agent.submission_policy import Channel

DESTINATION = "https://boards.greenhouse.io/acme/jobs/123"
SECRET = "s3cr3t-do-not-persist-9f2b"


class FakeTransport:
    """Transporte controlado: registra o pedido, devolve o que o teste mandar."""

    def __init__(self, response: ApiResponse | None = None, error: Exception | None = None):
        self.response = response or ApiResponse(200, b'{"id": 42, "status": "ok"}')
        self.error = error
        self.requests: list[ApiRequest] = []

    def send(self, request: ApiRequest) -> ApiResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def _ready_application(db: Database) -> str:
    job = Job(
        id="job-api",
        source="greenhouse",
        external_id="123",
        company="Acme",
        title="Engineer",
        description="Build software",
    )
    db.save_job(job, "greenhouse:123", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "safety_gate_evaluated")
    return application.id


def _form(tmp_path: Path, *, with_resume: bool = True) -> ApplicationForm:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 curriculum")
    fields = [
        ApplicationField(key="first_name", label="First name", required=True, value="Ada", source="profile"),
    ]
    if with_resume:
        fields.append(
            ApplicationField(
                key="resume",
                label="Resume",
                field_type="file",
                required=True,
                attachment_path=str(resume),
                source="profile",
            )
        )
    return ApplicationForm(
        form_id="form-api",
        provider="greenhouse",
        fields=fields,
        artifact_root=str(tmp_path),
    )


def _router(tmp_path: Path, transport: FakeTransport, **kwargs) -> tuple[SubmissionRouter, Database, str, ApplicationForm]:
    db = Database(tmp_path / "api.db")
    application_id = _ready_application(db)
    credential = ApiCredential(domain="greenhouse.io", secret=SECRET)
    router = SubmissionRouter(
        db,
        transport=transport,
        credentials={credential.domain: credential},
        **kwargs,
    )
    return router, db, application_id, _form(tmp_path)


def _submit(router: SubmissionRouter, db: Database, application_id: str, form: ApplicationForm, **kwargs) -> SubmissionResult:
    application = db.get_application(application_id)
    assert application is not None
    job = db.get_job(application.job_id)
    assert job is not None
    return router.submit(
        application=application,
        job=job,
        form=form,
        provider="greenhouse",
        destination=kwargs.pop("destination", DESTINATION),
        resume_path=kwargs.pop("resume_path", str(Path(form.artifact_root) / "resume.pdf")),
        resume_sha256="resume-v1",
        resume_field=kwargs.pop("resume_field", "resume"),
        form_fingerprint="form-v1",
        answers_fingerprint="answers-v1",
        **kwargs,
    )


# -- decisao de canal -----------------------------------------------------------


def test_without_a_declared_credential_the_channel_degrades_to_browser(tmp_path: Path):
    db = Database(tmp_path / "api.db")
    _ready_application(db)
    router = SubmissionRouter(db, transport=FakeTransport())
    assert router.channel_for(DESTINATION) is Channel.BROWSER
    assert router.credential_for(DESTINATION) is None
    with pytest.raises(ApiSubmissionError, match="no declared API credential"):
        _submit(router, db, _application_id(db), _form(tmp_path))


def _application_id(db: Database) -> str:
    applications = [row for job in db.list_jobs() for row in [db.get_application_for_job(job.id)] if row]
    assert applications
    return applications[0].id


def test_a_declared_credential_authorizes_the_api_channel(tmp_path: Path):
    transport = FakeTransport()
    router, _db, _application, _form_value = _router(tmp_path, transport)
    assert router.channel_for(DESTINATION) is Channel.API
    # LinkedIn continua HANDOFF mesmo com credencial declarada.
    router.credentials["linkedin.com"] = ApiCredential(domain="linkedin.com", secret=SECRET)
    assert router.channel_for("https://www.linkedin.com/jobs/view/1") is Channel.HANDOFF


def test_a_credential_for_another_domain_does_not_authorize_the_api_channel(tmp_path: Path):
    db = Database(tmp_path / "api.db")
    _ready_application(db)
    credential = ApiCredential(domain="lever.co", secret=SECRET)
    router = SubmissionRouter(db, transport=FakeTransport(), credentials={"lever.co": credential})
    assert router.channel_for(DESTINATION) is Channel.BROWSER
    with pytest.raises(ApiSubmissionError):
        _submit(router, db, _application_id(db), _form(tmp_path), credential=credential)


# -- guard proprio --------------------------------------------------------------


def test_the_write_budget_is_single_use():
    policy = ApiWritePolicy(provider="greenhouse", application_id="app-1", submission_intent_id="intent-1", destination=DESTINATION)
    guard = ApiWriteGuard(policy)
    guard.authorize("POST", DESTINATION)
    assert guard.writes_used == 1 and guard.exhausted
    with pytest.raises(ApiSubmissionError, match="budget exhausted"):
        guard.authorize("POST", DESTINATION)


@pytest.mark.parametrize(
    "method,url,stage",
    [
        ("GET", DESTINATION, "SUBMIT"),
        ("POST", "https://evil.example/acme/jobs/123", "SUBMIT"),
        ("POST", "https://boards.greenhouse.io/other/jobs/1", "SUBMIT"),
        ("POST", DESTINATION, "PREPARE"),
        ("POST", DESTINATION + "?next=evil", "SUBMIT"),
        ("POST", "http://boards.greenhouse.io/acme/jobs/123", "SUBMIT"),
    ],
)
def test_the_guard_refuses_anything_that_is_not_the_authorized_write(method: str, url: str, stage: str):
    policy = ApiWritePolicy(provider="greenhouse", application_id="app-1", submission_intent_id="intent-1", destination=DESTINATION)
    guard = ApiWriteGuard(policy)
    with pytest.raises(ApiSubmissionError):
        guard.authorize(method, url, stage)
    assert guard.writes_used == 0


def test_loopback_is_only_allowed_when_explicitly_authorized():
    loopback = "http://127.0.0.1:8000/jobs/1/apply"
    strict = ApiWritePolicy(provider="greenhouse", application_id="a", submission_intent_id="i", destination=loopback)
    assert not strict.validate("POST", loopback, "SUBMIT").valid
    allowed = ApiWritePolicy(
        provider="greenhouse", application_id="a", submission_intent_id="i", destination=loopback, allow_insecure_destination=True
    )
    assert allowed.validate("POST", loopback, "SUBMIT").valid


def test_the_guard_is_not_the_browser_guard():
    """Nomes e tipos proprios: um `page` nao existe neste canal."""
    policy = ApiWritePolicy(provider="greenhouse", application_id="a", submission_intent_id="i", destination=DESTINATION)
    guard = ApiWriteGuard(policy)
    assert not hasattr(guard, "arm")
    assert not hasattr(guard, "authorized_write_usage")
    assert isinstance(policy.validate("POST", DESTINATION, "SUBMIT"), ValidationResult)


# -- classificacao --------------------------------------------------------------


@pytest.mark.parametrize(
    "response,expected_status,expected_token,expected_state",
    [
        (ApiResponse(200, b'{"id": 1}'), "SUBMITTED", "", ApplicationState.SUBMITTED),
        (ApiResponse(201, b""), "SUBMITTED", "", ApplicationState.SUBMITTED),
        (ApiResponse(302, b"", {"location": "https://elsewhere.example/thanks"}), "SUBMIT_UNKNOWN", "api_redirect_not_followed", ApplicationState.SUBMIT_UNKNOWN),
        (ApiResponse(401, b'{"error": "HTTP Basic: Access denied"}'), "SUBMIT_FAILED", "api_auth_rejected", ApplicationState.SUBMIT_FAILED),
        (ApiResponse(403, b""), "SUBMIT_FAILED", "api_auth_rejected", ApplicationState.SUBMIT_FAILED),
        (ApiResponse(422, b""), "SUBMIT_FAILED", "api_provider_rejected", ApplicationState.SUBMIT_FAILED),
        (ApiResponse(429, b""), "SUBMIT_FAILED", "api_rate_limited", ApplicationState.SUBMIT_FAILED),
        (ApiResponse(500, b""), "SUBMIT_UNKNOWN", "api_server_error", ApplicationState.SUBMIT_UNKNOWN),
    ],
)
def test_the_response_classification_table(
    response: ApiResponse, expected_status: str, expected_token: str, expected_state: ApplicationState, tmp_path: Path
):
    """O status da execucao usa o MESMO vocabulario do canal de browser."""
    transport = FakeTransport(response)
    router, db, application_id, form = _router(tmp_path, transport)

    result = _submit(router, db, application_id, form)

    assert result.status == expected_status
    assert result.writes == 1
    assert result.http_status == response.status_code
    assert result.transport == "api"
    assert len(transport.requests) == 1
    application = db.get_application(application_id)
    assert application is not None and application.state is expected_state
    attempt = db.list_submission_attempts(application_id)[0]
    assert attempt.status == expected_state.value
    if expected_token:
        assert attempt.evidence["reason_token"] == expected_token
    assert "Authorization" not in json.dumps(attempt.evidence)


def test_a_timeout_after_the_request_is_unknown_and_never_retried(tmp_path: Path):
    transport = FakeTransport(error=TimeoutError("timed out"))
    router, db, application_id, form = _router(tmp_path, transport)

    result = _submit(router, db, application_id, form)

    assert result.status == "SUBMIT_UNKNOWN"
    assert result.writes == 1
    assert len(transport.requests) == 1
    application = db.get_application(application_id)
    assert application is not None and application.state is ApplicationState.SUBMIT_UNKNOWN
    attempts = db.list_submission_attempts(application_id)
    assert attempts[0].evidence["reason_token"] == "api_transport_error"
    # O banco guarda o MOTIVO (token fechado); o resumo da execucao carrega o par
    # escrito/nao-confirmado, que e o que o operador le para decidir.
    assert result.evidence["submit_write"] is True
    assert result.evidence["confirmed_submission"] is False


def test_a_confirmation_declares_the_basis_of_the_confirmation(tmp_path: Path):
    transport = FakeTransport(ApiResponse(200, b'{"application_id": "abc", "secret_field": "candidate data"}'))
    router, db, application_id, form = _router(tmp_path, transport)

    _submit(router, db, application_id, form)

    attempt = db.list_submission_attempts(application_id)[0]
    assert attempt.evidence["confirmation_type"] == "provider_http_response"
    assert attempt.evidence["status_code"] == 200
    # O corpo do provedor NAO entra na evidencia: chave sim, valor e dado do candidato.
    assert "secret_field" not in json.dumps(attempt.evidence)
    events = {event.event: event.payload for event in db.list_application_events(application_id)}
    assert events["submission_confirmed"]["evidence"]["confirmation_type"] == "provider_http_response"


def test_the_payload_carries_the_answers_and_the_resume_once(tmp_path: Path):
    transport = FakeTransport()
    router, db, application_id, form = _router(tmp_path, transport)

    _submit(router, db, application_id, form)

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == DESTINATION
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert request.fields["job_application[first_name]"] == "Ada"
    assert list(request.files) == ["job_application[resume]"]
    filename, content, mime = request.files["job_application[resume]"]
    assert filename == "resume.pdf" and content.startswith(b"%PDF") and mime


def test_the_resume_field_name_is_declared_by_the_caller(tmp_path: Path):
    """O nome do campo de curriculo e por provider e NAO foi medido: sem ele, falha."""
    transport = FakeTransport()
    router, db, application_id, form = _router(tmp_path, transport)
    with pytest.raises(ApiSubmissionError, match="resume field name"):
        _submit(router, db, application_id, form, resume_field="")
    assert transport.requests == []


# -- contabilidade de intent e tentativa ----------------------------------------


def test_intent_and_attempt_are_recorded_on_the_domain_ledger(tmp_path: Path):
    transport = FakeTransport()
    router, db, application_id, form = _router(tmp_path, transport)

    result = _submit(router, db, application_id, form)

    intent = db.get_submission_intent(result.intent_id)
    assert intent is not None
    assert intent.status == "SUBMITTED" and intent.authorized_at
    attempt = db.get_submission_attempt(result.attempt_id)
    assert attempt is not None and attempt.status == "SUBMITTED" and attempt.completed_at
    # A tentativa foi persistida ANTES da escrita: e ela que existe se o processo morrer.
    assert attempt.origin == "https://boards.greenhouse.io"
    assert attempt.path_hash


def test_a_second_submission_of_the_same_materials_is_blocked(tmp_path: Path):
    transport = FakeTransport()
    router, db, application_id, form = _router(tmp_path, transport)
    _submit(router, db, application_id, form)

    with pytest.raises((SubmissionBoundaryError, ApiSubmissionError)):
        _submit(router, db, application_id, form)

    assert len(transport.requests) == 1
    assert len(db.list_submission_attempts(application_id)) == 1


# -- segredo --------------------------------------------------------------------


def test_the_credential_secret_never_reaches_storage_events_or_repr(tmp_path: Path):
    transport = FakeTransport()
    router, db, application_id, form = _router(tmp_path, transport)
    credential = router.credentials["greenhouse.io"]

    _submit(router, db, application_id, form)

    assert SECRET not in repr(credential) and SECRET not in str(credential)
    assert credential.redacted() == {"credential_scheme": "bearer", "credential_domain": "greenhouse.io"}
    events = json.dumps([event.payload for event in db.list_application_events(application_id)])
    assert SECRET not in events
    attempts = json.dumps([attempt.evidence for attempt in db.list_submission_attempts(application_id)])
    assert SECRET not in attempts
    intent = db.get_submission_intent(db.list_submission_intents(application_id)[0].id)
    assert intent is not None and SECRET not in json.dumps(intent.__dict__ if hasattr(intent, "__dict__") else str(intent))
    files = [path for path in tmp_path.iterdir() if path.name.startswith("api.db")]
    assert files, "o banco deveria existir"
    for path in files:
        assert SECRET.encode() not in path.read_bytes(), f"segredo em {path.name}"


def test_a_credential_requires_domain_and_secret():
    with pytest.raises(ApiSubmissionError, match="domain"):
        ApiCredential(domain="", secret=SECRET)
    with pytest.raises(ApiSubmissionError, match="secret"):
        ApiCredential(domain="greenhouse.io", secret="")
    with pytest.raises(ApiSubmissionError, match="scheme"):
        ApiCredential(domain="greenhouse.io", secret=SECRET, scheme="header")
    with pytest.raises(ApiSubmissionError, match="username"):
        ApiCredential(domain="greenhouse.io", secret=SECRET, scheme="basic")


def test_basic_authentication_is_encoded_and_still_masked():
    credential = ApiCredential(domain="greenhouse.io", secret=SECRET, scheme="basic", username="partner")
    header, value = credential.header()
    assert header == "Authorization" and value.startswith("Basic ")
    assert SECRET not in value
    assert SECRET not in repr(credential)


# -- o canal nao alcanca o browser -----------------------------------------------


def test_the_api_channel_does_not_import_the_browser_the_relay_or_the_resolver():
    """Se o canal de API alcancar o browser, o guard paralelo deixa de ser paralelo."""
    source = Path(__file__).resolve().parent.parent / "src" / "jobsearch_agent" / "submission_api.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "playwright",
        "playwright.sync_api",
        "jobsearch_agent.browser",
        "jobsearch_agent.submission_browser",
        "jobsearch_agent.live_view",
        "jobsearch_agent.challenge_integration",
        "challenge_resolution",
    }
    # `httpx` so entra dentro do transporte real, e nao ha import de topo dele aqui.
    assert not (imported & forbidden), sorted(imported & forbidden)


def test_the_httpx_transport_never_follows_a_redirect():
    """Documentado no codigo E verificado: um redirect seria uma segunda escrita."""
    source = Path(__file__).resolve().parent.parent / "src" / "jobsearch_agent" / "submission_api.py"
    text = source.read_text(encoding="utf-8")
    assert "follow_redirects=False" in text


def test_every_reason_token_this_channel_can_emit_is_accepted_by_the_domain():
    """Ponte entre o canal e o dominio: token novo nao pode nascer recusado.

    `_redacted_evidence` recusa (levanta) um token que nao esteja no conjunto
    fechado. Se este canal emitir um `api_*` que o dominio nao declara, o defeito
    so apareceria com uma submissao real na mao — o pior momento possivel.
    """
    source = Path(__file__).resolve().parent.parent / "src" / "jobsearch_agent" / "submission_api.py"
    tokens = {node.value for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("api_")}
    assert tokens, "o canal deveria declarar ao menos um motivo de falha"
    assert tokens <= set(SAFE_REASON_TOKENS), sorted(tokens - set(SAFE_REASON_TOKENS))
    assert tokens <= set(API_REASON_TOKENS), sorted(tokens - set(API_REASON_TOKENS))
