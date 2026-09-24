"""JSA-CONF-006E/F: Gmail como fonte de evidencia — sem rede e sem credencial real.

O que importa aqui e que a integracao seja LEAST PRIVILEGE, que o segredo nao
vaze e que uma falha da API nunca seja lida como "nao houve confirmacao".
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.confirmation import (
    CONFIRMATION_WINDOW_TOLERANCE,
    ConfirmationReconciliationService,
    ConfirmationSourceUnavailable,
    EmailMessage,
)
from jobsearch_agent.integrations.email import (
    ALLOWED_SCOPES,
    GMAIL_READONLY_SCOPE,
    GmailApiClient,
    GmailAuthError,
    GmailConfigError,
    GmailEmailSource,
    GmailPaths,
    authorize,
    credentials_summary,
    load_credentials,
    load_token_document,
)
from jobsearch_agent.integrations.email.gmail import message_from_payload
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import Database

SENTINEL_BODY = "sentinel-body-4d18"
SENTINEL_SUBJECT = "sentinel-subject-91ab"
SENTINEL_NAME = "Sentinel Candidate"
CANDIDATE_EMAIL = "sentinel.person@example.invalid"

JOB_TITLE = "Senior Frontend Engineer"


def _minutes_ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _payload(
    message_id: str,
    *,
    sender: str = "no-reply@greenhouse.io",
    subject: str = "Thank you for applying",
    body: str = "We received your application.",
    minutes_ago: float = 2.0,
    mime: str = "text/plain",
    internal_date: bool = True,
    headers: bool = True,
    html_body: str | None = None,
) -> dict:
    parts = [{"mimeType": mime, "body": {"data": _b64(body)}}]
    if html_body is not None:
        parts = [
            {"mimeType": "text/plain", "body": {"data": _b64(body)}},
            {"mimeType": "text/html", "body": {"data": _b64(html_body)}},
        ]
    payload: dict = {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "payload": {
            "mimeType": "multipart/alternative" if len(parts) > 1 else mime,
            "headers": (
                [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
                if headers
                else []
            ),
            "parts": parts,
        },
    }
    if internal_date:
        payload["internalDate"] = str(int(_minutes_ago(minutes_ago).timestamp() * 1000))
    return payload


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class FakeGmailApi:
    """API de mentira com falha programavel. Nenhuma rede, nenhum token."""

    def __init__(self, payloads: list[dict], *, ids: list[str] | None = None, list_error=None, get_error=None):
        self.payloads = {payload["id"]: payload for payload in payloads}
        self.ids = list(ids if ids is not None else self.payloads)
        self.list_error = list_error
        self.get_error = get_error
        self.queries: list[tuple[float, int]] = []
        self.fetched: list[str] = []

    def list_message_ids(self, *, after: datetime, limit: int) -> list[str]:
        self.queries.append((after.timestamp(), limit))
        if self.list_error is not None:
            raise self.list_error
        return self.ids[:limit]

    def fetch_message(self, message_id: str) -> dict:
        self.fetched.append(message_id)
        if self.get_error is not None:
            raise self.get_error
        return self.payloads[message_id]


# --- o cenario real: Application aguardando confirmacao ------------------------


def _awaiting(tmp_path: Path, suffix: str = "gmail") -> tuple[Database, str, Job]:
    database = Database(tmp_path / f"{suffix}.db")
    job = Job(
        id=f"job-{suffix}",
        source="greenhouse",
        external_id="12345",
        company="Acme",
        title=JOB_TITLE,
        description="Build the web app",
        url="https://boards.greenhouse.io/acme/jobs/12345",
    )
    database.save_job(job, f"greenhouse:{suffix}", {})
    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state in (
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
        ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED,
        ApplicationState.SUBMITTING,
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
    ):
        application = service.transition(application.id, state, f"to_{state.value.casefold()}")
    service.start_human_handoff(application.id)
    service.report_manual_submission(application.id)
    return database, application.id, job


def _state(database: Database, application_id: str) -> str:
    return database.get_application(application_id).state.value


# --- 006B: o porto generico ---------------------------------------------------


def test_the_source_returns_only_the_generic_contract():
    api = FakeGmailApi([_payload("18f0a1b2c3d4e5f6")])
    messages = GmailEmailSource(api).messages_since(_minutes_ago(10))

    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, EmailMessage)
    assert message.reference == "18f0a1b2c3d4e5f6"
    assert message.sender == "no-reply@greenhouse.io"
    assert message.subject == "Thank you for applying"
    assert message.body == "We received your application."
    # Nenhum payload da API sobrevive ao contrato.
    assert "payload" not in message.__dataclass_fields__


def test_the_provider_label_is_gmail():
    assert GmailEmailSource(FakeGmailApi([])).provider == "gmail"


def test_html_only_confirmation_is_read_without_tags():
    payload = _payload("m1", mime="text/html", body="<html><body><p>We received your <b>application</b>.</p></body></html>")
    message = message_from_payload("m1", payload, since=_minutes_ago(10))
    assert message is not None
    assert "<" not in message.body
    assert "We received your application" in message.body


def test_multipart_prefers_plain_text():
    payload = _payload("m1", body="We received your application.", html_body="<p>other</p>")
    message = message_from_payload("m1", payload, since=_minutes_ago(10))
    assert message is not None
    assert message.body == "We received your application."


# --- 006C: a janela e decidida pelo sistema ------------------------------------


def test_the_query_only_reduces_the_universe():
    """A busca do Gmail nao decide a janela: o timestamp real decide."""
    since = _minutes_ago(CONFIRMATION_WINDOW_TOLERANCE.total_seconds() / 60 - 1)
    api = FakeGmailApi(
        [
            _payload("inside", minutes_ago=2),
            _payload("outside-because-the-query-was-looser", minutes_ago=60 * 24 * 3),
        ]
    )
    messages = GmailEmailSource(api).messages_since(since)

    assert [message.reference for message in messages] == ["inside"]
    # A query foi enviada com o `since` do sistema.
    assert api.queries == [(since.timestamp(), 50)]


def test_a_message_before_the_window_is_never_evidence(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "old-message")
    api = FakeGmailApi([_payload("old", minutes_ago=60 * 24 * 2)])
    result = ConfirmationReconciliationService(database).reconcile(
        application_id, email_sources=[GmailEmailSource(api)]
    )
    assert result.detected == ()
    assert _state(database, application_id) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    database.close()


def test_a_message_without_a_timestamp_is_not_evidence():
    payload = _payload("m1", internal_date=False)
    assert message_from_payload("m1", payload, since=_minutes_ago(10)) is None


def test_the_same_message_twice_is_read_once():
    api = FakeGmailApi([_payload("dup")], ids=["dup", "dup", "dup"])
    messages = GmailEmailSource(api).messages_since(_minutes_ago(10))
    assert [message.reference for message in messages] == ["dup"]
    assert api.fetched == ["dup"]


# --- falha nao e ausencia ------------------------------------------------------


def test_a_listing_failure_is_not_absence_of_confirmation(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "list-fail")
    api = FakeGmailApi([], list_error=ConfirmationSourceUnavailable("gmail list failed: HttpError"))
    with pytest.raises(ConfirmationSourceUnavailable, match="source unavailable"):
        ConfirmationReconciliationService(database).reconcile(
            application_id, email_sources=[GmailEmailSource(api)]
        )
    assert _state(database, application_id) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    assert database.list_confirmation_evidence(application_id) == []
    database.close()


def test_a_fetch_failure_is_not_absence_of_confirmation(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "get-fail")
    api = FakeGmailApi([_payload("m1")], get_error=ConfirmationSourceUnavailable("gmail get failed: TimeoutError"))
    with pytest.raises(ConfirmationSourceUnavailable):
        ConfirmationReconciliationService(database).reconcile(
            application_id, email_sources=[GmailEmailSource(api)]
        )
    assert _state(database, application_id) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    assert database.list_confirmation_evidence(application_id) == []
    database.close()


def test_an_empty_mailbox_is_a_result_and_not_a_failure(tmp_path):
    """Caixa vazia e "olhei e nao havia": resultado, nao erro."""
    database, application_id, _job = _awaiting(tmp_path, "empty")
    result = ConfirmationReconciliationService(database).reconcile(
        application_id, email_sources=[GmailEmailSource(FakeGmailApi([]))]
    )
    assert result.accepted is None
    assert "no confirmation evidence" in result.detail
    database.close()


# --- 006D: portoes de credencial ----------------------------------------------


def _credential_files(
    tmp_path: Path,
    *,
    scopes=None,
    mode=0o600,
    token: bool = True,
    expiry: str = "2026-09-24T20:00:00Z",
) -> GmailPaths:
    paths = GmailPaths.default(tmp_path / "gmail")
    paths.directory.mkdir(parents=True, exist_ok=True)
    paths.directory.chmod(0o700)
    paths.client_secret.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cid.apps.googleusercontent.com",
                    "project_id": "local",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_secret": "client-secret-value",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    paths.client_secret.chmod(0o600)
    if token:
        paths.token.write_text(
            json.dumps(
                {
                    "token": "access-value",
                    "refresh_token": "refresh-value",
                    "client_id": "client",
                    "client_secret": "secret",
                    "scopes": scopes if scopes is not None else [GMAIL_READONLY_SCOPE],
                    "expiry": expiry,
                }
            ),
            encoding="utf-8",
        )
        paths.token.chmod(mode)
    return paths


def test_only_the_readonly_scope_is_requested():
    assert ALLOWED_SCOPES == {GMAIL_READONLY_SCOPE}
    assert "gmail.modify" not in ALLOWED_SCOPES
    assert "gmail.send" not in ALLOWED_SCOPES


def test_a_world_readable_token_is_refused(tmp_path):
    paths = _credential_files(tmp_path, mode=0o644)
    with pytest.raises(GmailConfigError, match="group/other"):
        load_token_document(paths.token)


def test_a_token_with_broader_scopes_is_refused(tmp_path):
    paths = _credential_files(tmp_path, scopes=[GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify"])
    with pytest.raises(GmailAuthError, match="beyond"):
        load_token_document(paths.token)


def test_a_missing_credential_is_refused(tmp_path):
    paths = _credential_files(tmp_path, token=False)
    with pytest.raises(GmailConfigError, match="missing"):
        load_token_document(paths.token)


def test_the_summary_never_reveals_the_secret(tmp_path):
    paths = _credential_files(tmp_path)
    summary = credentials_summary(paths)
    serialized = json.dumps(summary)
    assert summary["authorized"] is True
    assert summary["scopes"] == [GMAIL_READONLY_SCOPE]
    assert set(summary) == {
        "directory",
        "client_secret_present",
        "token_present",
        "scope",
        "authorized",
        "detail",
        "scopes",
        "expiry",
    }
    # Nenhum VALOR de credencial: o resumo diz que esta autorizado, nao com o que.
    for secret in ("access-value", "refresh-value", "granted-access", "granted-refresh"):
        assert secret not in serialized
    assert "token" not in serialized.replace("token_present", "")


class _FakeCredentials:
    def __init__(self, *, expired: bool, refresh_token: str = "refresh-value", fail: bool = False):
        self.expired = expired
        self.refresh_token = refresh_token
        self.fail = fail
        self.refreshed = 0

    def refresh(self, request):
        self.refreshed += 1
        if self.fail:
            raise RuntimeError("invalid_grant")
        self.expired = False

    def to_json(self):
        return json.dumps({"token": "new-access", "refresh_token": self.refresh_token, "scopes": [GMAIL_READONLY_SCOPE]})


def test_an_expired_token_is_refreshed_and_saved_privately(tmp_path):
    paths = _credential_files(tmp_path)
    credentials = _FakeCredentials(expired=True)
    loaded = load_credentials(
        paths, credentials_factory=lambda document: credentials, request_factory=lambda: object()
    )
    assert loaded is credentials
    assert credentials.refreshed == 1
    assert (paths.token.stat().st_mode & 0o777) == 0o600
    assert "new-access" in paths.token.read_text(encoding="utf-8")


def test_a_refused_refresh_is_an_unavailable_integration(tmp_path):
    paths = _credential_files(tmp_path)
    credentials = _FakeCredentials(expired=True, fail=True)
    with pytest.raises(GmailAuthError, match="refused"):
        load_credentials(
            paths, credentials_factory=lambda document: credentials, request_factory=lambda: object()
        )


def test_a_valid_token_is_used_without_refreshing(tmp_path):
    paths = _credential_files(tmp_path)
    credentials = _FakeCredentials(expired=False)
    load_credentials(paths, credentials_factory=lambda document: credentials, request_factory=lambda: object())
    assert credentials.refreshed == 0


class _FakeFlow:
    def __init__(self, credentials):
        self.credentials = credentials
        self.ports: list[int] = []

    def run_local_server(self, *, port: int, open_browser: bool):
        self.ports.append(port)
        return self.credentials


class _GrantedCredentials:
    def __init__(self, scopes):
        self.scopes = scopes

    def to_json(self):
        return json.dumps(
            {"token": "granted-access", "refresh_token": "granted-refresh", "scopes": self.scopes}
        )


def test_authorize_saves_a_private_token_and_returns_no_secret(tmp_path):
    paths = _credential_files(tmp_path, token=False)
    summary = authorize(
        paths,
        flow_factory=lambda client, scopes: _FakeFlow(_GrantedCredentials(scopes)),
    )
    assert summary["authorized"] is True
    assert summary["scopes"] == [GMAIL_READONLY_SCOPE]
    assert "granted-access" not in json.dumps(summary)
    assert "granted-refresh" not in json.dumps(summary)
    assert (paths.token.stat().st_mode & 0o777) == 0o600
    assert (paths.directory.stat().st_mode & 0o777) == 0o700
    assert load_token_document(paths.token)["refresh_token"] == "granted-refresh"


def test_authorize_refuses_a_broader_grant_and_saves_nothing(tmp_path):
    paths = _credential_files(tmp_path, token=False)
    granted = [GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify"]
    with pytest.raises(GmailAuthError, match="beyond readonly"):
        authorize(paths, flow_factory=lambda client, scopes: _FakeFlow(_GrantedCredentials(granted)))
    assert not paths.token.exists()


def test_authorize_requires_the_client_secret(tmp_path):
    paths = GmailPaths.default(tmp_path / "gmail")
    with pytest.raises(GmailConfigError, match="client_secret.json"):
        authorize(paths)


def test_the_api_client_wraps_transport_failures(tmp_path):
    """Um erro do googleapiclient nao pode escapar como se fosse resposta."""
    class Exploding:
        def users(self):
            raise RuntimeError("boom: 401")

    client = GmailApiClient(credentials=object(), service=Exploding())
    with pytest.raises(ConfirmationSourceUnavailable, match="gmail list failed"):
        client.list_message_ids(after=_minutes_ago(10), limit=5)
    with pytest.raises(ConfirmationSourceUnavailable, match="gmail get failed"):
        client.fetch_message("m1")


# --- 006E/F: reconciliacao de ponta a ponta ------------------------------------


def test_a_strong_gmail_message_confirms_the_application(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "strong")
    api = FakeGmailApi([_payload("18f0a1b2c3d4e5f6", sender="no-reply@greenhouse.io")])
    result = ConfirmationReconciliationService(database).reconcile(
        application_id, email_sources=[GmailEmailSource(api)]
    )

    assert result.accepted is not None
    assert result.accepted["provider"] == "gmail"
    assert result.accepted["reference"] == "18f0a1b2c3d4e5f6"
    assert result.accepted["source"] == "confirmation_email"
    assert result.accepted["signals"] == ["application_confirmation_phrase", "ats_domain_match", "time_window_match"]
    assert _state(database, application_id) == ApplicationState.SUBMITTED.value
    stored = database.list_confirmation_evidence(application_id)
    assert len(stored) == 1
    assert stored[0]["accepted"] is True
    database.close()


def test_a_weak_gmail_message_is_persisted_and_refused(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "weak")
    api = FakeGmailApi([_payload("weak-1", sender="newsletter@example.invalid")])
    result = ConfirmationReconciliationService(database).reconcile(
        application_id, email_sources=[GmailEmailSource(api)]
    )

    assert result.accepted is None
    assert "too weak" in result.detail
    assert _state(database, application_id) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    stored = database.list_confirmation_evidence(application_id)
    assert len(stored) == 1 and stored[0]["accepted"] is False
    database.close()


def test_reconciling_twice_with_the_same_message_does_not_duplicate(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "dedup")
    api = FakeGmailApi([_payload("dup-1", sender="newsletter@example.invalid")])
    service = ConfirmationReconciliationService(database)
    service.reconcile(application_id, email_sources=[GmailEmailSource(api)])
    service.reconcile(application_id, email_sources=[GmailEmailSource(api)])

    assert len(database.list_confirmation_evidence(application_id)) == 1
    database.close()


def test_nothing_from_the_message_survives_into_the_record(tmp_path):
    database, application_id, _job = _awaiting(tmp_path, "sentinels")
    api = FakeGmailApi(
        [
            _payload(
                "18f0a1b2c3d4e5f6",
                sender=f"{SENTINEL_NAME} via Greenhouse <no-reply@greenhouse.io>",
                subject=SENTINEL_SUBJECT,
                body=(
                    f"We received your application for {JOB_TITLE}. "
                    f"Write to {CANDIDATE_EMAIL}. {SENTINEL_BODY}"
                ),
            )
        ]
    )
    result = ConfirmationReconciliationService(database).reconcile(
        application_id, email_sources=[GmailEmailSource(api)]
    )

    assert result.accepted is not None
    persisted = json.dumps(database.list_confirmation_evidence(application_id))
    journal = json.dumps([event.payload for event in database.list_application_events(application_id)])
    serialized = persisted + journal + json.dumps(result.to_dict())
    for secret in (SENTINEL_BODY, SENTINEL_SUBJECT, SENTINEL_NAME, CANDIDATE_EMAIL, "no-reply@greenhouse.io"):
        assert secret not in serialized, f"vazou {secret!r}"
    # O que fica e a evidencia: referencia opaca, instante, score e sinais.
    assert result.accepted["reference"] == "18f0a1b2c3d4e5f6"
    assert "job_match" in result.accepted["signals"]
    database.close()


# --- as fabricas REAIS, sem rede (bibliotecas opcionais) ------------------------


def test_the_real_google_credentials_factory_accepts_a_readonly_setup(tmp_path):
    """Liga o modulo as bibliotecas de verdade, sem chamar a API.

    Roda so quando o grupo opcional esta instalado: e a checagem de fiacao entre
    `token.json` e `google.oauth2.credentials`, que nenhum teste com fabrica
    injetada alcanca.
    """
    pytest.importorskip("google.oauth2.credentials")
    paths = _credential_files(tmp_path, expiry="2099-01-01T00:00:00Z")

    credentials = load_credentials(paths)
    assert credentials.expired is False
    assert list(credentials.scopes) == [GMAIL_READONLY_SCOPE]


def test_the_real_google_flow_factory_reads_the_client_secret(tmp_path):
    pytest.importorskip("google_auth_oauthlib.flow")
    from jobsearch_agent.integrations.email.oauth import _google_flow_factory

    paths = _credential_files(tmp_path, token=False)
    flow = _google_flow_factory(str(paths.client_secret), [GMAIL_READONLY_SCOPE])
    assert type(flow).__name__ == "InstalledAppFlow"
    # O fluxo carregou o client secret e guardou o escopo pedido.
    assert flow.oauth2session.scope == [GMAIL_READONLY_SCOPE]


# --- gate de 006F: acesso real sem ler conteudo --------------------------------


def test_the_access_check_reads_no_message_at_all():
    """Lista apenas ids: nenhum assunto, remetente ou corpo entra no processo."""
    from jobsearch_agent.integrations.email import verify_read_access

    api = FakeGmailApi(
        [_payload("18f0a1b2c3d4e5f6", sender=f"No Reply <no-reply@greenhouse.io>", subject=SENTINEL_SUBJECT, body=SENTINEL_BODY)]
    )
    summary = verify_read_access(api)

    assert summary["api_reachable"] is True
    assert summary["messages_listed"] == 1
    assert summary["messages_read"] == 0
    assert api.fetched == [], "o check leu uma mensagem inteira"
    serialized = json.dumps(summary)
    for secret in (SENTINEL_SUBJECT, SENTINEL_BODY, "no-reply@greenhouse.io"):
        assert secret not in serialized


def test_the_access_check_fails_loudly_instead_of_reporting_success():
    from jobsearch_agent.integrations.email import verify_read_access

    api = FakeGmailApi([], list_error=ConfirmationSourceUnavailable("gmail list failed: HttpError"))
    with pytest.raises(ConfirmationSourceUnavailable):
        verify_read_access(api)


def test_the_check_cli_prints_no_message_content(tmp_path, capsys, monkeypatch):
    """O comando inteiro, com a credencial e a API trocadas por dublês."""
    from jobsearch_agent.cli import main as cli_main
    from jobsearch_agent.integrations.email import oauth

    paths = _credential_files(tmp_path, expiry="2099-01-01T00:00:00Z")
    api = FakeGmailApi([_payload("18f0a1b2c3d4e5f6", subject=SENTINEL_SUBJECT, body=SENTINEL_BODY)])
    monkeypatch.setattr(oauth, "_google_credentials_factory", lambda document: _FakeCredentials(expired=False))
    monkeypatch.setattr(
        "jobsearch_agent.integrations.email.GmailApiClient", lambda credentials: api
    )

    code = cli_main(["--root", str(tmp_path), "integrations", "gmail", "check", "--gmail-dir", str(paths.directory)])
    printed = capsys.readouterr().out
    assert code == 0
    assert json.loads(printed)["messages_read"] == 0
    assert SENTINEL_SUBJECT not in printed and SENTINEL_BODY not in printed
