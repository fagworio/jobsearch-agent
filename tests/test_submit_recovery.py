"""P0.2 — uma unica interpretacao autorizada de tentativa estranda.

O ponto central: **campo ausente NAO e campo vazio**. Uma tentativa criada antes
do P0.1 nao tem `write_possible_at` — e pode ter chegado ao POST — entao ela e
AMBIGUA. So uma tentativa do codigo novo, com a chave presente e vazia, pode ser
declarada seguramente pre-write. O dataclass apaga essa diferenca, entao a decisao
le o `attempt_json` BRUTO.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import ApplicationConflict, Database
from jobsearch_agent.submission import LiveNetworkPolicy, SubmissionService, build_review_snapshot
from jobsearch_agent.serialization import canonical_json


def _stranded(tmp_path: Path) -> tuple[Database, str, str]:
    """Application + intent + tentativa, no estado SUBMITTING (estrada real)."""
    database = Database(tmp_path / "recovery.db")
    job = Job(id="job-r", source="lever", external_id="1", company="Acme", title="Engineer", description="x")
    database.save_job(job, "lever:1", {})
    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state, event in (
        (ApplicationState.PREPARING, "application_preparing"),
        (ApplicationState.MATERIALS_READY, "materials_ready"),
        (ApplicationState.READY_TO_APPLY, "safety_gate_evaluated"),
    ):
        service.transition(application.id, state, event)
    submissions = SubmissionService(database)
    intent = submissions.create_intent(
        application_id=application.id, job_id=job.id, provider="lever",
        destination="https://jobs.lever.co/acme/1/apply", form_fingerprint="form-v1",
        resume_sha256="resume-v1", answers_fingerprint="answers-v1", expires_in_seconds=300,
    )
    submissions.save_review_snapshot(
        build_review_snapshot(
            application_id=application.id, job_id=job.id, company="Acme", title="Engineer",
            provider="lever", destination=intent.destination, resume_filename="resume.pdf",
            resume_sha256="resume-v1", form_fingerprint="form-v1", answers_fingerprint="answers-v1",
        )
    )
    submissions.authorize_submission(intent.id)
    policy = LiveNetworkPolicy(
        provider="lever", allowed_origin="https://jobs.lever.co", allowed_path_pattern=r"^/acme/1/apply$",
        allowed_method="POST", allowed_stage="SUBMIT", application_id=application.id,
        submission_intent_id=intent.id,
    )
    attempt = submissions.begin_submission(
        intent.id, current_form_fingerprint="form-v1", current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1", policy=policy, method="POST", url=intent.destination,
    )
    assert database.get_application(application.id).state is ApplicationState.SUBMITTING
    return database, application.id, attempt.id


def _raw(database: Database, attempt_id: str) -> dict:
    row = database.connection.execute("SELECT attempt_json FROM submission_attempts WHERE id=?", (attempt_id,)).fetchone()
    return json.loads(row[0])


def _write_raw(database: Database, attempt_id: str, payload: dict) -> None:
    database.connection.execute(
        "UPDATE submission_attempts SET attempt_json=? WHERE id=?", (canonical_json(payload), attempt_id)
    )
    database.connection.commit()


def _events(database: Database, application_id: str) -> dict:
    return {event.event: dict(event.payload) for event in database.list_application_events(application_id)}


def test_a_new_code_attempt_with_an_explicit_empty_marker_is_safely_pre_write(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    assert "write_possible_at" in _raw(database, attempt_id), "o codigo novo sempre grava a chave"
    assert _raw(database, attempt_id)["write_possible_at"] == ""

    branch = database.recover_stranded_submission(application_id)

    assert branch == "pre_write"
    assert database.get_application(application_id).state is ApplicationState.REVIEW_REACHED
    assert database.get_submission_attempt(attempt_id).status == "INTERRUPTED"
    events = _events(database, application_id)
    assert events["submission_recovery_pre_write"]["action"] == "resume"


def test_a_legacy_attempt_without_the_key_is_ambigUOUS_never_pre_write(tmp_path: Path):
    """O caso que o dataclass esconderia: chave AUSENTE e tentativa do codigo velho.

    Ela pode ter chegado ao POST sem nunca marcar nada. Tratar isso como
    "certamente pre-write" reabriria a duplicidade que o P0 existe para fechar.
    """
    database, application_id, attempt_id = _stranded(tmp_path)
    payload = _raw(database, attempt_id)
    del payload["write_possible_at"]
    _write_raw(database, attempt_id, payload)

    branch = database.recover_stranded_submission(application_id)

    assert branch == "unknown"
    assert database.get_application(application_id).state is ApplicationState.SUBMIT_UNKNOWN
    assert database.get_submission_attempt(attempt_id).status == "SUBMIT_UNKNOWN"
    events = _events(database, application_id)
    assert events["submission_recovery_unknown"]["action"] == "reconcile_confirmation"
    assert "submission_recovery_pre_write" not in events


def test_a_crossed_boundary_is_ambiguous(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    payload = _raw(database, attempt_id)
    payload["write_possible_at"] = "2026-09-25T10:00:00+00:00"
    _write_raw(database, attempt_id, payload)

    assert database.recover_stranded_submission(application_id) == "unknown"
    assert database.get_application(application_id).state is ApplicationState.SUBMIT_UNKNOWN


def test_a_non_submitting_application_is_a_noop(tmp_path: Path):
    database, application_id, _attempt = _stranded(tmp_path)
    database.recover_stranded_submission(application_id)  # leva para REVIEW_REACHED
    assert database.recover_stranded_submission(application_id) == "noop"


def test_zero_active_attempts_fail_closed(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    database.connection.execute("UPDATE submission_attempts SET status='FAILED' WHERE id=?", (attempt_id,))
    database.connection.commit()

    with pytest.raises(ApplicationConflict, match="exactly one active attempt"):
        database.recover_stranded_submission(application_id)
    assert database.get_application(application_id).state is ApplicationState.SUBMITTING, "nada foi tocado"


def test_two_active_attempts_fail_closed(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    raw = _raw(database, attempt_id)
    database.connection.execute(
        "INSERT INTO submission_attempts(id, intent_id, application_id, provider, method, origin, path_hash, status, attempt_json, started_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("attempt-duplicada", raw["intent_id"], application_id, "lever", "POST", "https://jobs.lever.co", "abc",
         "SUBMITTING", canonical_json(raw), raw.get("started_at", ""), ""),
    )
    database.connection.commit()

    with pytest.raises(ApplicationConflict, match="exactly one active attempt"):
        database.recover_stranded_submission(application_id)


def test_an_attempt_pointing_to_an_unknown_intent_fail_closed(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    intent_id = _raw(database, attempt_id)["intent_id"]
    # Simula banco inconsistente (FK desligada, importacao externa): o guard tem
    # de falhar fechado mesmo quando o SQLite nao impede a corrupcao.
    database.connection.execute("PRAGMA foreign_keys=OFF")
    database.connection.execute("UPDATE submission_attempts SET intent_id='intent-fantasma' WHERE id=?", (attempt_id,))
    database.connection.commit()
    database.connection.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(ApplicationConflict, match="unknown intent"):
        database.recover_stranded_submission(application_id)
