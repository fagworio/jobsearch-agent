"""P0.2 — uma unica interpretacao autorizada de tentativa estranda.

O ponto central: **campo ausente NAO e campo vazio**. Uma tentativa criada antes
do P0.1 nao tem `write_possible_at` — e pode ter chegado ao POST — entao ela e
AMBIGUA. So uma tentativa do codigo novo, com a chave presente e vazia, pode ser
declarada seguramente pre-write. O dataclass apaga essa diferenca, entao a decisao
le o `attempt_json` BRUTO.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import ApplicationConflict, Database
from jobsearch_agent.submission import LiveNetworkPolicy, SubmissionService, build_review_snapshot
from jobsearch_agent.submission import SubmissionBoundaryError
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
    # FONTE DE VERDADE do dominio: coluna e JSON sincronizados.
    assert database.get_submission_intent(_raw(database, attempt_id)["intent_id"]).status == "INTERRUPTED"
    assert events["submission_recovery_pre_write"]["write_boundary"] == "pre_write"


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
    # A auditoria nao pode apagar a diferenca que decidiu o ramo: chave AUSENTE.
    assert events["submission_recovery_unknown"]["write_boundary"] == "legacy_missing"
    assert events["submission_recovery_unknown"]["write_possible_at_present"] is False
    assert database.get_submission_intent(payload["intent_id"]).status == "SUBMIT_UNKNOWN"


def _race(database: Database, mutation) -> None:
    """Simula OUTRO processo alterando o estado entre a leitura e o CAS."""
    import jobsearch_agent.persistence as persistence

    original = persistence.json.loads
    calls = {"n": 0}

    def hooked(raw, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            mutation()
        return original(raw, *args, **kwargs)

    persistence.json.loads = hooked
    try:
        yield_call = None
    finally:
        persistence.json.loads = original


def test_losing_the_attempt_race_rolls_everything_back(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    intent_id = _raw(database, attempt_id)["intent_id"]
    import sqlite3

    from jobsearch_agent import persistence as persistence_module

    original = persistence_module.json.loads
    state = {"mutated": False}

    def hooked(raw, *args, **kwargs):
        if not state["mutated"]:
            state["mutated"] = True
            other = sqlite3.connect(database.path)
            other.execute("UPDATE submission_attempts SET attempt_json='{\"status\": \"SUBMITTING\"}' WHERE id=?", (attempt_id,))
            other.commit()
            other.close()
        return original(raw, *args, **kwargs)

    persistence_module.json.loads = hooked
    try:
        with pytest.raises(ApplicationConflict, match="lost attempt race|diverges"):
            database.recover_stranded_submission(application_id)
    finally:
        persistence_module.json.loads = original

    assert database.get_application(application_id).state is ApplicationState.SUBMITTING
    assert database.get_submission_intent(intent_id).status == "SUBMITTING", "rollback completo"
    assert "submission_recovery_unknown" not in _events(database, application_id)


def test_losing_the_application_race_rolls_everything_back(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    import sqlite3

    from jobsearch_agent import persistence as persistence_module

    original = persistence_module.json.loads
    state = {"mutated": False}

    def hooked(raw, *args, **kwargs):
        if not state["mutated"]:
            state["mutated"] = True
            other = sqlite3.connect(database.path)
            other.execute("UPDATE applications SET state='SUBMIT_UNKNOWN' WHERE id=?", (application_id,))
            other.commit()
            other.close()
        return original(raw, *args, **kwargs)

    persistence_module.json.loads = hooked
    try:
        with pytest.raises(ApplicationConflict, match="lost application race"):
            database.recover_stranded_submission(application_id)
    finally:
        persistence_module.json.loads = original

    assert database.get_submission_attempt(attempt_id).status == "SUBMITTING", "rollback completo"
    assert database.get_submission_intent(_raw(database, attempt_id)["intent_id"]).status == "SUBMITTING"


def test_an_attempt_json_divergent_from_its_columns_fail_closed(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    payload = _raw(database, attempt_id)
    payload["id"] = "attempt-fantasma"
    _write_raw(database, attempt_id, payload)

    with pytest.raises(ApplicationConflict, match="diverges"):
        database.recover_stranded_submission(application_id)
    assert database.get_application(application_id).state is ApplicationState.SUBMITTING


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
    payload = _raw(database, attempt_id)
    payload["intent_id"] = "intent-fantasma"
    database.connection.execute(
        "UPDATE submission_attempts SET intent_id='intent-fantasma', attempt_json=? WHERE id=?",
        (canonical_json(payload), attempt_id),
    )
    database.connection.commit()
    database.connection.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(ApplicationConflict, match="unknown intent"):
        database.recover_stranded_submission(application_id)


# --- P0.2b: rearm seguro da MESMA intent (id deterministico) -------------------


def _retry_flow(database: Database, application_id: str) -> tuple[str, str]:
    """create_intent de novo com os MESMOS fingerprints: rearm + authorize + begin."""
    service = SubmissionService(database)
    intent = service.create_intent(
        application_id=application_id, job_id="job-r", provider="lever",
        destination="https://jobs.lever.co/acme/1/apply", form_fingerprint="form-v1",
        resume_sha256="resume-v1", answers_fingerprint="answers-v1", expires_in_seconds=300,
    )
    service.authorize_submission(intent.id)
    policy = LiveNetworkPolicy(
        provider="lever", allowed_origin="https://jobs.lever.co", allowed_path_pattern=r"^/acme/1/apply$",
        allowed_method="POST", allowed_stage="SUBMIT", application_id=application_id,
        submission_intent_id=intent.id,
    )
    # A boundary exige snapshot de review; o da primeira tentativa continua valido.
    from jobsearch_agent.submission import build_review_snapshot

    service.save_review_snapshot(
        build_review_snapshot(
            application_id=application_id, job_id="job-r", company="Acme", title="Engineer",
            provider="lever", destination=intent.destination, resume_filename="resume.pdf",
            resume_sha256="resume-v1", form_fingerprint="form-v1", answers_fingerprint="answers-v1",
        )
    )
    attempt = service.begin_submission(
        intent.id, current_form_fingerprint="form-v1", current_resume_sha256="resume-v1",
        current_answers_fingerprint="answers-v1", policy=policy, method="POST", url=intent.destination,
    )
    return intent.id, attempt.id


def test_a_proven_pre_write_crash_really_resumes(tmp_path: Path):
    """O ponto do P0.2b: chegar a REVIEW_REACHED nao basta, tem de dar para retomar.

    A intent tem ID DETERMINISTICO: sem rearm, a retomada encontrava a mesma intent
    em INTERRUPTED e `authorize_submission` recusava — arquitetura correta no banco
    e produto incapaz de enviar curriculo.
    """
    database, application_id, first_attempt = _stranded(tmp_path)
    intent_id = _raw(database, first_attempt)["intent_id"]
    assert database.recover_stranded_submission(application_id) == "pre_write"
    assert database.get_application(application_id).state is ApplicationState.REVIEW_REACHED
    assert database.get_submission_intent(intent_id).status == "INTERRUPTED"

    new_intent_id, new_attempt_id = _retry_flow(database, application_id)

    assert new_intent_id == intent_id, "a identidade deterministica e reaproveitada"
    assert database.get_submission_intent(intent_id).status == "SUBMITTING"
    assert database.get_submission_attempt(first_attempt).status == "INTERRUPTED"
    assert database.get_submission_attempt(new_attempt_id).status == "SUBMITTING"
    assert database.get_submission_attempt(new_attempt_id).write_possible_at == ""
    assert len(database.list_submission_attempts(application_id)) == 2, "uma tentativa nova, a antiga preservada"


def test_a_legacy_interrupted_intent_is_never_rearmed(tmp_path: Path):
    """Registro produzido pela logica antiga (chave AUSENTE) nao rearma."""
    database, application_id, attempt_id = _stranded(tmp_path)
    payload = _raw(database, attempt_id)
    del payload["write_possible_at"]
    _write_raw(database, attempt_id, payload)
    database.recover_stranded_submission(application_id)  # -> unknown (ambiguo)

    # Forca o estado INTERRUPTED (como a logica antiga de retry_submit deixava) e
    # tenta rearmar: a prova persistida e que decide.
    database.connection.execute("UPDATE submission_intents SET status='INTERRUPTED' WHERE application_id=?", (application_id,))
    database.connection.commit()
    from jobsearch_agent.submission import SubmissionBoundaryError

    with pytest.raises(SubmissionBoundaryError):
        SubmissionService(database).create_intent(
            application_id=application_id, job_id="job-r", provider="lever",
            destination="https://jobs.lever.co/acme/1/apply", form_fingerprint="form-v1",
            resume_sha256="resume-v1", answers_fingerprint="answers-v1", expires_in_seconds=300,
        )


def test_a_crossed_boundary_never_rearms(tmp_path: Path):
    database, application_id, attempt_id = _stranded(tmp_path)
    payload = _raw(database, attempt_id)
    payload["write_possible_at"] = "2026-09-25T10:00:00+00:00"
    _write_raw(database, attempt_id, payload)
    database.recover_stranded_submission(application_id)  # -> unknown
    database.connection.execute("UPDATE submission_intents SET status='INTERRUPTED' WHERE application_id=?", (application_id,))
    database.connection.commit()
    from jobsearch_agent.submission import SubmissionBoundaryError

    with pytest.raises(SubmissionBoundaryError):
        SubmissionService(database).create_intent(
            application_id=application_id, job_id="job-r", provider="lever",
            destination="https://jobs.lever.co/acme/1/apply", form_fingerprint="form-v1",
            resume_sha256="resume-v1", answers_fingerprint="answers-v1", expires_in_seconds=300,
        )


def _review_ready(tmp_path: Path) -> tuple[Database, str]:
    """Application em READY_TO_APPLY, sem intent e sem attempt: a porta aceita."""
    database = Database(tmp_path / "matrix.db")
    job = Job(id="job-m", source="lever", external_id="1", company="Acme", title="Engineer", description="x")
    database.save_job(job, "lever:1", {})
    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state, event in (
        (ApplicationState.PREPARING, "application_preparing"),
        (ApplicationState.MATERIALS_READY, "materials_ready"),
        (ApplicationState.READY_TO_APPLY, "safety_gate_evaluated"),
    ):
        service.transition(application.id, state, event)
    return database, application.id


def _create(database: Database, application_id: str, *, fingerprint: str = "form-v1"):
    # O job_id vem da PROPRIA Application: inventar um id fixo fazia
    # `create_intent` recusar em "application and job do not match" antes de
    # sequer chegar ao rearm — e o teste acusava o rearm por um erro de fixture.
    application = database.get_application(application_id)
    return SubmissionService(database).create_intent(
        application_id=application_id, job_id=application.job_id, provider="lever",
        destination="https://jobs.lever.co/acme/1/apply", form_fingerprint=fingerprint,
        resume_sha256="resume-v1", answers_fingerprint="answers-v1", expires_in_seconds=300,
    )


def test_the_first_creation_from_a_ready_application_is_plain_created(tmp_path: Path):
    database, application_id = _review_ready(tmp_path)

    intent = _create(database, application_id)

    assert intent.status == "CREATED"
    assert database.list_submission_attempts(application_id) == []
    assert database.get_submission_intent(intent.id).status == "CREATED"


def test_an_existing_created_intent_is_returned_unchanged(tmp_path: Path):
    """Idempotencia: criar de novo com os mesmos fingerprints nao muda nada.

    Os estados AUTHORIZED/SUBMITTING/SUBMITTED nao entram nesta matriz porque a
    propria porta os recusa: `create_intent` gateia a Application antes de olhar a
    intent existente, e `begin_submission` move Application e intent juntas. Nao se
    fabrica uma combinacao que o dominio nao produz.
    """
    database, application_id = _review_ready(tmp_path)
    first = _create(database, application_id)

    second = _create(database, application_id)

    assert second.id == first.id
    assert second.status == "CREATED"
    assert second.authorized_at == first.authorized_at == ""
    assert database.list_submission_attempts(application_id) == []
    assert len(database.list_submission_intents(application_id)) == 1


def test_losing_the_rearm_race_fails_closed(tmp_path: Path):
    """Corrida REAL: o perdedor nao escreve por cima do vencedor.

    Nada de adulterar registro: o estado INTERRUPTED vem do proprio recovery, e a
    segunda conexao representa o PROCESSO VENCEDOR rearmando de forma consistente
    (coluna e JSON). O perdedor tenta o CAS com o snapshot antigo e recebe
    rowcount = 0. O rollback do perdedor nao desfaz o vencedor — ele e outro
    processo —, e o que se prova e que ele nao sobrescreve.
    """
    database, application_id, attempt_id = _stranded(tmp_path)
    intent_id = _raw(database, attempt_id)["intent_id"]
    assert database.recover_stranded_submission(application_id) == "pre_write"
    assert database.get_submission_intent(intent_id).status == "INTERRUPTED"
    assert len(database.list_submission_attempts(application_id)) == 1

    # PROCESSO VENCEDOR (outra conexao): INTERRUPTED -> CREATED, coluna e JSON.
    raw = database.connection.execute(
        "SELECT intent_json FROM submission_intents WHERE id=?", (intent_id,)
    ).fetchone()[0]
    payload = json.loads(raw)
    payload["status"] = "CREATED"
    payload["authorized_at"] = ""
    winner = sqlite3.connect(database.path)
    winner.execute(
        "UPDATE submission_intents SET status='CREATED', intent_json=?, authorized_at='' WHERE id=?",
        (canonical_json(payload), intent_id),
    )
    winner.commit()
    winner.close()

    # PERDEDOR: CAS com o snapshot antigo (INTERRUPTED) -> zero linhas.
    assert database.rearm_submission_intent(intent_id, expires_at="2030-01-01T00:00:00+00:00") is False

    final = database.get_submission_intent(intent_id)
    assert final.status == "CREATED", "o vencedor sobrevive; o perdedor nao escreve por cima"
    assert final.expires_at != "2030-01-01T00:00:00+00:00", "a validade do perdedor nao foi aplicada"
    assert database.get_application(application_id).state is ApplicationState.REVIEW_REACHED
    attempts = database.list_submission_attempts(application_id)
    assert len(attempts) == 1 and attempts[0].status == "INTERRUPTED"
    assert len(database.list_submission_intents(application_id)) == 1, "mesmo ID deterministico"
    assert not any(
        event.event.startswith("submission_recovery") and "authorized" in event.event
        for event in database.list_application_events(application_id)
    )


class RearmFakeReached(Exception):
    """Sentinela: prova que a porta chegou ao rearm (medicao, nao contrato)."""


def test_the_domain_rearm_fails_closed_when_the_cas_is_lost(tmp_path: Path, monkeypatch):
    """Medicao do caminho ate o CAS, e depois o contrato da recusa.

    A primeira fase levanta uma sentinela para provar que o fake e alcancado;
    "nao levantou" e "levantou por outro motivo" ficam indistinguiveis sem isso.
    """
    called = {"value": False}

    def lose_rearm(*args, **kwargs):
        called["value"] = True
        # `False` SEM levantar: prova que o codigo posterior ao CAS trata a
        # recusa. Com sentinela, o caminho pos-chamada nem seria executado.
        return False

    database, application_id, attempt_id = _stranded(tmp_path)
    intent_id = _raw(database, attempt_id)["intent_id"]
    assert database.recover_stranded_submission(application_id) == "pre_write"

    # FIXTURE validado antes da chamada: se `_create` falhar antes do fake, o
    # estado de entrada estava correto e a busca fica nos guards da porta.
    application = database.get_application(application_id)
    intent = database.get_submission_intent(intent_id)
    attempts = database.list_submission_attempts(application_id)
    assert application.state is ApplicationState.REVIEW_REACHED
    assert intent.status == "INTERRUPTED"
    assert len(attempts) == 1 and attempts[0].status == "INTERRUPTED"
    raw = json.loads(database.raw_attempt_json(attempts[0].id))
    assert "write_possible_at" in raw and raw["write_possible_at"] == ""

    monkeypatch.setattr(database, "rearm_submission_intent", lose_rearm)

    # CONTAGENS antes: a candidatura JA teve `submission_authorized` legitimo na
    # primeira tentativa, entao "nenhum submission_authorized" seria uma assercao
    # errada. A propriedade e "CAS=False -> zero efeitos DURAVEIS novos".
    events_before = len(database.list_application_events(application_id))
    attempts_before = len(database.list_submission_attempts(application_id))

    # Sem `match`: para o P0 a propriedade importa, nao a frase da excecao.
    with pytest.raises(SubmissionBoundaryError):
        _create(database, application_id)

    assert called["value"] is True, "o CAS nao foi alcancado"
    # Tudo relido do BANCO (objeto em memoria nao e fonte de verdade).
    assert database.get_application(application_id).state is ApplicationState.REVIEW_REACHED
    assert database.get_submission_intent(intent_id).status == "INTERRUPTED"
    attempts_after = database.list_submission_attempts(application_id)
    assert len(attempts_after) == attempts_before == 1
    assert attempts_after[0].status == "INTERRUPTED"
    assert len(database.list_application_events(application_id)) == events_before, "nenhum evento novo"
