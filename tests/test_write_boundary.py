"""P0.1 — a fronteira de risco da escrita.

O marcador `write_possible_at` e o que separa "pode retomar" de "NUNCA reenviar":

    vazio      -> a escrita ainda nao era possivel; um crash aqui e seguro
    preenchido -> o POST pode ter saido; um crash aqui e ambiguidade

Ele precisa ser MONOTONO (`"" -> timestamp`, uma vez) e gravado ANTES de armar a
escrita — nunca depois, porque a janela entre armar e marcar e exatamente a janela
em que o POST sai sem deixar rastro.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.models import ApplicationState, Job, SubmissionAttempt
from jobsearch_agent.persistence import Database


def _attempt(tmp_path: Path, *, insert: bool = True) -> tuple[Database, str, str]:
    """Attempt com intent REAL: `submission_attempts.intent_id` tem FK.

    `insert=False` prepara Application + intent + attempt em memoria, sem gravar a
    tentativa — o caminho de quem vai passar por `begin_submission` de verdade.
    """
    database = Database(tmp_path / "boundary.db")
    job = Job(id="job-w", source="lever", external_id="1", company="Acme", title="Engineer", description="x")
    database.save_job(job, "lever:1", {})
    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state, event in (
        (ApplicationState.PREPARING, "application_preparing"),
        (ApplicationState.MATERIALS_READY, "materials_ready"),
        (ApplicationState.READY_TO_APPLY, "safety_gate_evaluated"),
    ):
        service.transition(application.id, state, event)
    from jobsearch_agent.submission import SubmissionService

    intent = SubmissionService(database).create_intent(
        application_id=application.id,
        job_id=job.id,
        provider="lever",
        destination="https://jobs.lever.co/acme/1/apply",
        form_fingerprint="form-v1",
        resume_sha256="resume-v1",
        answers_fingerprint="answers-v1",
        expires_in_seconds=300,
    )
    attempt = SubmissionAttempt(
        id="attempt-1", intent_id=intent.id, application_id=application.id, provider="lever", method="POST",
        origin="https://jobs.lever.co", path_hash="abc",
    )
    from jobsearch_agent.serialization import canonical_json

    columns = [
        row[1]
        for row in database.connection.execute("PRAGMA table_info(submission_attempts)")
        if row[3] == 1 and row[1] != "id"
    ]
    if not insert:
        return database, attempt.id, application.id
    payload = attempt.__dict__
    values = [canonical_json(payload) if column.endswith("_json") else str(payload.get(column, "")) for column in columns]
    database.connection.execute(
        f"INSERT INTO submission_attempts(id,{','.join(columns)}) VALUES (?,{','.join('?' for _ in columns)})",
        (attempt.id, *values),
    )
    database.connection.commit()
    return database, attempt.id, application.id


def test_the_marker_starts_empty_and_is_crossed_once(tmp_path: Path):
    database, attempt_id, _app = _attempt(tmp_path)
    assert database.get_submission_attempt(attempt_id).write_possible_at == ""

    first = database.mark_write_possible(attempt_id, at="2026-09-25T10:00:00+00:00")
    second = database.mark_write_possible(attempt_id, at="2026-09-25T10:00:05+00:00")

    assert first is True
    assert second is False, "a fronteira nunca e cruzada duas vezes"
    assert database.get_submission_attempt(attempt_id).write_possible_at == "2026-09-25T10:00:00+00:00"


def test_the_marker_refuses_an_unknown_or_finished_attempt(tmp_path: Path):
    database, attempt_id, _app = _attempt(tmp_path)
    assert database.mark_write_possible("attempt-inexistente") is False

    payload = __import__("json").loads(
        database.connection.execute("SELECT attempt_json FROM submission_attempts WHERE id=?", (attempt_id,)).fetchone()[0]
    )
    payload["status"] = "SUBMITTED"
    payload["write_possible_at"] = ""
    from jobsearch_agent.serialization import canonical_json

    database.connection.execute(
        "UPDATE submission_attempts SET attempt_json=? WHERE id=?", (canonical_json(payload), attempt_id)
    )
    database.connection.commit()
    assert database.mark_write_possible(attempt_id) is False, "tentativa com desfecho nao cruza fronteira"


def test_a_legacy_attempt_deserializes_without_the_field():
    legacy = {
        "id": "attempt-legado", "intent_id": "intent-1", "application_id": "app", "provider": "lever",
        "method": "POST", "origin": "https://jobs.lever.co", "path_hash": "abc", "status": "SUBMITTING",
    }
    attempt = SubmissionAttempt(**legacy)
    assert attempt.write_possible_at == "", "registro anterior ao campo nao pode quebrar"


def test_the_marker_is_written_before_the_write_is_armed():
    """Ordem verificada no codigo: marcar DEPOIS de armar reabriria a janela."""
    source = Path(__file__).resolve().parent.parent / "src" / "jobsearch_agent" / "submission_browser.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    marker_line = arm_line = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "mark_write_possible":
                marker_line = node.lineno
            elif node.func.attr == "arm_writes":
                arm_line = node.lineno
    assert marker_line is not None, "o submitter precisa persistir a fronteira"
    assert arm_line is not None
    assert marker_line < arm_line, f"mark_write_possible (l.{marker_line}) tem de vir ANTES de arm_writes (l.{arm_line})"


# --- fail-closed: o CAS precisa IMPEDIR o proximo passo ------------------------


class _RecordingSession:
    """Sessao minima: registra se `arm_writes` foi chamado e quantos POSTs sairam."""

    def __init__(self) -> None:
        self.arm_calls = 0
        self.posts = 0
        self.page = object()
        self.network_guard = None

    def arm_writes(self, permits: object) -> None:
        self.arm_calls += 1

    def arm_challenge_runtime(self, permits: object) -> None:
        return None

    def disarm_authorized_write(self, *args: object, **kwargs: object) -> None:
        return None

    def disarm_challenge_runtime(self, *args: object, **kwargs: object) -> None:
        return None

    def close(self) -> None:
        return None


def test_the_cas_result_controls_whether_the_write_is_armed(tmp_path: Path, monkeypatch):
    """CAS False => `arm_writes` nao pode ser chamado, e nenhum POST sai.

    Teste COMPORTAMENTAL: substitui o CAS por um que recusa e verifica que o passo
    seguinte nao acontece. Antes disto, o retorno era ignorado e a escrita era
    armada sem prova de que a fronteira existia.
    """
    # Sem tentativa pre-gravada: quem cria a tentativa e `begin_submission`.
    database, _unused, application_id = _attempt(tmp_path, insert=False)
    session = _RecordingSession()
    from jobsearch_agent.submission import LiveNetworkPolicy, SubmissionService
    from jobsearch_agent.submission_browser import BrowserSubmitter

    intent = database.list_submission_intents(application_id)[0]
    service = SubmissionService(database)
    from jobsearch_agent.submission import build_review_snapshot

    service.save_review_snapshot(
        build_review_snapshot(
            application_id=application_id,
            job_id="job-w",
            company="Acme",
            title="Engineer",
            provider="lever",
            destination=intent.destination,
            resume_filename="resume.pdf",
            resume_sha256="resume-v1",
            form_fingerprint="form-v1",
            answers_fingerprint="answers-v1",
        )
    )
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy(
        provider="lever",
        allowed_origin="https://jobs.lever.co",
        allowed_path_pattern=r"^/acme/1/apply$",
        allowed_method="POST",
        allowed_stage="SUBMIT",
        application_id=application_id,
        submission_intent_id=intent.id,
    )
    # A tentativa e criada PELO submitter (que e o caminho de producao); aqui so
    # a intent fica autorizada.
    monkeypatch.setattr(database, "mark_write_possible", lambda *a, **k: False)

    from jobsearch_agent.submission import SubmissionBoundaryError

    with pytest.raises(SubmissionBoundaryError, match="write boundary could not be persisted"):
        BrowserSubmitter(database, timeout_seconds=0.1).submit(
            session,
            intent.id,
            current_form_fingerprint="form-v1",
            current_resume_sha256="resume-v1",
            current_answers_fingerprint="answers-v1",
            policy=policy,
        )

    assert session.arm_calls == 0, "sem fronteira persistida, nada e armado"
    assert session.posts == 0
    attempts = database.list_submission_attempts(application_id)
    assert attempts and attempts[-1].write_possible_at == "", "a fronteira nao foi cruzada"
