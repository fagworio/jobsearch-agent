"""SQLite persistence with versioned, conservative migrations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import Application, ApplicationEvent, ApplicationState, Job, JobState, ReviewSnapshot, SubmissionAttempt, SubmissionIntent, now_iso
from .serialization import canonical_json
from .sources import JobIdentityCandidate, identity_records


class ApplicationConflict(RuntimeError):
    """Raised when another process changed an Application first."""


def _migration_001_initial(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            canonical_key TEXT NOT NULL UNIQUE,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            job_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS analyses (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id),
            analysis_json TEXT NOT NULL,
            fit_json TEXT,
            strategy_json TEXT,
            resume_json TEXT,
            validation_json TEXT,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            event TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )


def _legacy_identity_table(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("PRAGMA table_info(job_identities)").fetchall()
    return bool(rows) and {str(row[1]) for row in rows} >= {"identity", "job_id"}


def _insert_identity(connection: sqlite3.Connection, job_id: str, candidate: JobIdentityCandidate) -> None:
    try:
        connection.execute(
            """INSERT INTO job_identities
               (job_id, identity_type, identity_value, strength, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, candidate.identity_type, candidate.identity_value, candidate.strength, candidate.source, now_iso()),
        )
    except sqlite3.IntegrityError:
        # Contradictory legacy data remains lossless and reviewable.
        connection.execute(
            """INSERT OR IGNORE INTO job_identities
               (job_id, identity_type, identity_value, strength, source, created_at)
               VALUES (?, ?, ?, 'weak', ?, ?)""",
            (job_id, f"legacy_conflict:{candidate.identity_type}", candidate.identity_value, candidate.source, now_iso()),
        )


def _record_possible_duplicate(connection: sqlite3.Connection, job_id: str, candidate_job_id: str, reason: str, evidence: dict[str, Any]) -> None:
    first, second = sorted((job_id, candidate_job_id))
    if first == second:
        return
    connection.execute(
        """INSERT INTO duplicate_candidates
           (job_id, candidate_job_id, reason, confidence, evidence_json, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
           ON CONFLICT(job_id, candidate_job_id) DO UPDATE SET
             reason=excluded.reason,
             confidence=MAX(duplicate_candidates.confidence, excluded.confidence),
             evidence_json=excluded.evidence_json,
             updated_at=excluded.updated_at""",
        (first, second, reason, 0.5, canonical_json(evidence), now_iso(), now_iso()),
    )


def _migration_002_identities(connection: sqlite3.Connection) -> None:
    legacy_table = False
    if _legacy_identity_table(connection):
        connection.execute("ALTER TABLE job_identities RENAME TO job_identities_legacy")
        legacy_table = True
    connection.execute(
        """CREATE TABLE IF NOT EXISTS job_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            identity_type TEXT NOT NULL,
            identity_value TEXT NOT NULL,
            strength TEXT NOT NULL CHECK (strength IN ('strong', 'weak')),
            source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(job_id, identity_type, identity_value)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS duplicate_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            candidate_job_id TEXT NOT NULL REFERENCES jobs(id),
            reason TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            evidence_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, candidate_job_id),
            CHECK(job_id <> candidate_job_id)
        )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_job_identity_strong
           ON job_identities(identity_type, identity_value)
           WHERE strength = 'strong'"""
    )

    if legacy_table:
        rows = connection.execute("SELECT identity, job_id FROM job_identities_legacy").fetchall()
        for row in rows:
            connection.execute(
                """INSERT OR IGNORE INTO job_identities
                   (job_id, identity_type, identity_value, strength, source, created_at)
                   VALUES (?, 'legacy', ?, 'weak', 'legacy', ?)""",
                (str(row[1]), str(row[0]), now_iso()),
            )

    rows = connection.execute("SELECT id, job_json FROM jobs ORDER BY created_at, id").fetchall()
    for row in rows:
        job_id = str(row[0])
        data = json.loads(row[1])
        data["state"] = JobState(data.get("state", JobState.DISCOVERED))
        job = Job(**data)
        for candidate in identity_records(job):
            _insert_identity(connection, job_id, candidate)

    weak_rows = connection.execute(
        """SELECT identity_type, identity_value, GROUP_CONCAT(job_id)
           FROM job_identities
           WHERE strength = 'weak'
           GROUP BY identity_type, identity_value
           HAVING COUNT(DISTINCT job_id) > 1"""
    ).fetchall()
    for identity_type, identity_value, job_ids in weak_rows:
        ids = sorted(set(str(job_id) for job_id in str(job_ids).split(",")))
        for index, job_id in enumerate(ids):
            for candidate_job_id in ids[index + 1 :]:
                _record_possible_duplicate(connection, job_id, candidate_job_id, f"weak_identity:{identity_type}", {"identity_type": identity_type, "identity_value": identity_value})


def _migration_003_applications(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            state TEXT NOT NULL,
            context_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_application_job ON applications(job_id)")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id TEXT NOT NULL REFERENCES applications(id),
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            event TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS application_answers (
            application_id TEXT NOT NULL REFERENCES applications(id),
            question_key TEXT NOT NULL,
            answer_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(application_id, question_key)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS application_forms (
            application_id TEXT PRIMARY KEY REFERENCES applications(id),
            form_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _migration_004_submission_boundary(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS submission_intents (
            id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL REFERENCES applications(id),
            job_id TEXT NOT NULL REFERENCES jobs(id),
            provider TEXT NOT NULL,
            destination TEXT NOT NULL,
            form_fingerprint TEXT NOT NULL,
            resume_sha256 TEXT NOT NULL,
            answers_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            intent_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            authorized_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS review_snapshots (
            application_id TEXT PRIMARY KEY REFERENCES applications(id),
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS submission_attempts (
            id TEXT PRIMARY KEY,
            intent_id TEXT NOT NULL REFERENCES submission_intents(id),
            application_id TEXT NOT NULL REFERENCES applications(id),
            provider TEXT NOT NULL,
            method TEXT NOT NULL,
            origin TEXT NOT NULL,
            path_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_submission_attempts_application
           ON submission_attempts(application_id, started_at)"""
    )


def _migration_005_human_handoff(connection: sqlite3.Connection) -> None:
    """Pacote de handoff humano (JSA-CG-016).

    Append-only por construcao: o id e derivado do conteudo do pacote, entao um
    material diferente gera outra linha, e a anterior continua auditavel. Nao ha
    UPDATE nesta tabela.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS human_handoff_packages (
            id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL REFERENCES applications(id),
            job_id TEXT NOT NULL REFERENCES jobs(id),
            package_sha256 TEXT NOT NULL,
            package_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_handoff_packages_application
           ON human_handoff_packages(application_id, created_at)"""
    )


def _migration_006_confirmation_evidence(connection: sqlite3.Connection) -> None:
    """Evidencia de confirmacao OBSERVADA (JSA-CONF-004).

    Append-only e separada do journal de estados, porque "detectada" e
    "aceita" sao coisas diferentes: uma evidencia fraca e registrada e recusada,
    e a linha continua dizendo o que foi observado e por que nao bastou. Nao ha
    DELETE: evidencia observada nao se apaga, no maximo deixa de ser aceita.
    """
    connection.execute(
        """CREATE TABLE IF NOT EXISTS confirmation_evidence (
            id TEXT PRIMARY KEY,
            application_id TEXT NOT NULL REFERENCES applications(id),
            source TEXT NOT NULL,
            provider TEXT NOT NULL,
            confidence REAL NOT NULL,
            accepted INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_confirmation_evidence_application
           ON confirmation_evidence(application_id, observed_at)"""
    )


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "initial", _migration_001_initial),
    (2, "namespaced_identities_and_duplicate_candidates", _migration_002_identities),
    (3, "application_domain_and_events", _migration_003_applications),
    (4, "submission_boundary", _migration_004_submission_boundary),
    (5, "human_handoff_packages", _migration_005_human_handoff),
    (6, "confirmation_evidence", _migration_006_confirmation_evidence),
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )"""
        )
        self._run_migrations()

    def _run_migrations(self) -> None:
        applied = {int(row[0]) for row in self.connection.execute("SELECT version FROM schema_migrations")}
        for version, name, migration in MIGRATIONS:
            if version in applied:
                continue
            with self.connection:
                migration(self.connection)
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, now_iso()),
                )

    def close(self) -> None:
        self.connection.close()

    def save_job(self, job: Job, canonical_key: str, raw_payload: dict[str, Any]) -> Job:
        payload = canonical_json(raw_payload)
        records = identity_records(job)
        strong_values = [candidate.identity_value for candidate in records if candidate.strength == "strong"]
        existing = self.connection.execute("SELECT id FROM jobs WHERE canonical_key = ?", (canonical_key,)).fetchone()
        if not existing and strong_values:
            placeholders = ",".join("?" for _ in strong_values)
            existing = self.connection.execute(
                f"""SELECT job_id AS id FROM job_identities
                    WHERE strength = 'strong' AND identity_value IN ({placeholders})
                    ORDER BY id LIMIT 1""",
                strong_values,
            ).fetchone()
        if existing:
            job.id = str(existing[0])
        data = canonical_json(job)
        if existing:
            self.connection.execute("UPDATE jobs SET job_json=?, payload_json=?, state=?, updated_at=? WHERE id=?", (data, payload, job.state.value, job.discovered_at, job.id))
        else:
            self.connection.execute(
                """INSERT INTO jobs
                   (id,source,external_id,canonical_key,company,title,url,state,payload_json,job_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.source, job.external_id, canonical_key, job.company, job.title, job.url, job.state.value, payload, data, job.discovered_at, job.discovered_at),
            )
        for candidate in records:
            _insert_identity(self.connection, job.id, candidate)
            if candidate.strength == "weak":
                matches = self.connection.execute(
                    """SELECT DISTINCT job_id FROM job_identities
                       WHERE identity_type=? AND identity_value=? AND strength='weak' AND job_id<>?""",
                    (candidate.identity_type, candidate.identity_value, job.id),
                ).fetchall()
                for match in matches:
                    _record_possible_duplicate(self.connection, job.id, str(match[0]), f"weak_identity:{candidate.identity_type}", {"identity_type": candidate.identity_type, "identity_value": candidate.identity_value})
        self.connection.commit()
        return job

    def get_job(self, job_id: str) -> Job | None:
        row = self.connection.execute("SELECT job_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["state"] = JobState(data.get("state", JobState.DISCOVERED))
        return Job(**data)

    def list_jobs(self) -> list[Job]:
        rows = self.connection.execute("SELECT job_json FROM jobs ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            data = json.loads(row[0])
            data["state"] = JobState(data.get("state", JobState.DISCOVERED))
            result.append(Job(**data))
        return result

    def list_duplicate_candidates(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM duplicate_candidates WHERE status=? ORDER BY created_at", (status,)).fetchall()
        return [dict(row) for row in rows]

    def save_application(self, application: Application) -> None:
        payload = canonical_json(application.context)
        self.connection.execute(
            """INSERT INTO applications(id, job_id, state, context_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 state=excluded.state,
                 context_json=excluded.context_json,
                 updated_at=excluded.updated_at""",
            (application.id, application.job_id, application.state.value, payload, application.created_at, application.updated_at),
        )
        self.connection.commit()

    def get_application(self, application_id: str) -> Application | None:
        row = self.connection.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
        if not row:
            return None
        return Application(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            state=ApplicationState(row["state"]),
            context=json.loads(row["context_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_application_for_job(self, job_id: str) -> Application | None:
        row = self.connection.execute("SELECT id FROM applications WHERE job_id=?", (job_id,)).fetchone()
        return self.get_application(str(row[0])) if row else None

    def record_application_event(self, event: ApplicationEvent) -> None:
        self.connection.execute(
            """INSERT INTO application_events
               (application_id, from_state, to_state, event, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event.application_id, event.from_state.value, event.to_state.value, event.event, canonical_json(event.payload), event.created_at),
        )
        self.connection.commit()

    def save_application_transition(self, application: Application, event: ApplicationEvent) -> None:
        """Persist state and its audit event atomically."""
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE applications SET state=?, context_json=?, updated_at=?
                   WHERE id=? AND state=?""",
                (application.state.value, canonical_json(application.context), application.updated_at, application.id, event.from_state.value),
            )
            # The event carries the expected source state. Re-read it in the
            # predicate so two workers cannot both win from the same state.
            if cursor.rowcount != 1:
                raise ApplicationConflict(f"application transition lost race: {application.id}")
            self.connection.execute(
                """INSERT INTO application_events
                   (application_id, from_state, to_state, event, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event.application_id, event.from_state.value, event.to_state.value, event.event, canonical_json(event.payload), event.created_at),
            )

    def append_application_event(self, application_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        """Evento de AUDITORIA: registra sem mudar estado.

        Usado pelo handoff do operador, cuja evidencia precisa sobreviver a um
        restart mesmo quando nao ha transicao de estado para gravar (a janela
        abre e fecha com a Application no mesmo estado).
        """
        row = self.connection.execute("SELECT state FROM applications WHERE id=?", (application_id,)).fetchone()
        if row is None:
            raise ApplicationConflict(f"application not found: {application_id}")
        state = str(row[0])
        self.connection.execute(
            """INSERT INTO application_events
               (application_id, from_state, to_state, event, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (application_id, state, state, event, canonical_json(payload or {}), now_iso()),
        )
        self.connection.commit()

    def list_application_events(self, application_id: str) -> list[ApplicationEvent]:
        rows = self.connection.execute("SELECT * FROM application_events WHERE application_id=? ORDER BY id", (application_id,)).fetchall()
        return [ApplicationEvent(str(row["application_id"]), ApplicationState(row["from_state"]), ApplicationState(row["to_state"]), str(row["event"]), json.loads(row["payload_json"]), str(row["created_at"])) for row in rows]

    def save_application_answer(self, application_id: str, answer: Any) -> None:
        self.connection.execute(
            """INSERT INTO application_answers(application_id, question_key, answer_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(application_id, question_key) DO UPDATE SET
                 answer_json=excluded.answer_json,
                 updated_at=excluded.updated_at""",
            (application_id, answer.question_key, canonical_json(answer), now_iso()),
        )
        self.connection.commit()

    def list_application_answers(self, application_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT answer_json FROM application_answers WHERE application_id=? ORDER BY question_key", (application_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_application_form(self, application_id: str, form: Any) -> None:
        self.connection.execute(
            """INSERT INTO application_forms(application_id, form_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(application_id) DO UPDATE SET
                 form_json=excluded.form_json,
                 updated_at=excluded.updated_at""",
            (application_id, canonical_json(form), now_iso()),
        )
        self.connection.commit()

    def get_application_form(self, application_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT form_json FROM application_forms WHERE application_id=?", (application_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_submission_intent(self, intent: SubmissionIntent) -> None:
        self.connection.execute(
            """INSERT INTO submission_intents
               (id, application_id, job_id, provider, destination,
                form_fingerprint, resume_sha256, answers_fingerprint, status,
                intent_json, created_at, expires_at, authorized_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 status=excluded.status,
                 intent_json=excluded.intent_json,
                 authorized_at=excluded.authorized_at""",
            (
                intent.id,
                intent.application_id,
                intent.job_id,
                intent.provider,
                intent.destination,
                intent.form_fingerprint,
                intent.resume_sha256,
                intent.answers_fingerprint,
                intent.status,
                canonical_json(intent),
                intent.created_at,
                intent.expires_at,
                intent.authorized_at,
            ),
        )
        self.connection.commit()

    def get_submission_intent(self, intent_id: str) -> SubmissionIntent | None:
        row = self.connection.execute("SELECT intent_json FROM submission_intents WHERE id=?", (intent_id,)).fetchone()
        return SubmissionIntent(**json.loads(row[0])) if row else None

    def list_submission_intents(self, application_id: str) -> list[SubmissionIntent]:
        rows = self.connection.execute(
            "SELECT intent_json FROM submission_intents WHERE application_id=? ORDER BY created_at",
            (application_id,),
        ).fetchall()
        return [SubmissionIntent(**json.loads(row[0])) for row in rows]

    def save_review_snapshot(self, snapshot: ReviewSnapshot) -> None:
        self.connection.execute(
            """INSERT INTO review_snapshots(application_id, snapshot_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(application_id) DO UPDATE SET
                 snapshot_json=excluded.snapshot_json,
                 updated_at=excluded.updated_at""",
            (snapshot.application_id, canonical_json(snapshot), now_iso()),
        )
        self.connection.commit()

    def get_review_snapshot(self, application_id: str) -> ReviewSnapshot | None:
        row = self.connection.execute("SELECT snapshot_json FROM review_snapshots WHERE application_id=?", (application_id,)).fetchone()
        return ReviewSnapshot(**json.loads(row[0])) if row else None

    def get_submission_attempt(self, attempt_id: str) -> SubmissionAttempt | None:
        row = self.connection.execute("SELECT attempt_json FROM submission_attempts WHERE id=?", (attempt_id,)).fetchone()
        return SubmissionAttempt(**json.loads(row[0])) if row else None

    def list_submission_attempts(self, application_id: str) -> list[SubmissionAttempt]:
        rows = self.connection.execute(
            "SELECT attempt_json FROM submission_attempts WHERE application_id=? ORDER BY started_at",
            (application_id,),
        ).fetchall()
        return [SubmissionAttempt(**json.loads(row[0])) for row in rows]

    def mark_write_possible(self, attempt_id: str, *, at: str = "") -> bool:
        """Cruza a fronteira de risco da escrita: monotono e condicional.

        Nao e "SELECT, modifica em Python, UPDATE sem condicao": a condicao viaja
        no proprio UPDATE (`attempt_json=?`), e antes dela o payload e conferido
        (`status == SUBMITTING` e `write_possible_at == ""`). Quem perde a
        corrida recebe `False`, e uma fronteira cruzada nunca e descruzada.
        """
        moment = at or now_iso()
        with self.connection:
            row = self.connection.execute(
                "SELECT attempt_json FROM submission_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row[0])
            if str(payload.get("status", "")) != "SUBMITTING":
                return False
            if str(payload.get("write_possible_at", "")):
                return False
            payload["write_possible_at"] = moment
            cursor = self.connection.execute(
                "UPDATE submission_attempts SET attempt_json=? WHERE id=? AND attempt_json=?",
                (canonical_json(payload), attempt_id, row[0]),
            )
            return cursor.rowcount == 1

    def _set_intent_status(self, intent_id: str, status: str) -> None:
        """Atualiza o status da intent DENTRO da transacao corrente."""
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(submission_intents)")}
        if "status" in columns:
            self.connection.execute("UPDATE submission_intents SET status=? WHERE id=?", (status, intent_id))
            return
        row = self.connection.execute("SELECT intent_json FROM submission_intents WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            return
        payload = json.loads(row[0])
        payload["status"] = status
        self.connection.execute(
            "UPDATE submission_intents SET intent_json=? WHERE id=? AND intent_json=?",
            (canonical_json(payload), intent_id, row[0]),
        )

    #: Ramo do recovery de uma tentativa estranda.
    RECOVERY_NOOP = "noop"
    RECOVERY_PRE_WRITE = "pre_write"
    RECOVERY_UNKNOWN = "unknown"

    def recover_stranded_submission(self, application_id: str) -> str:
        """Unica interpretacao autorizada de uma tentativa estranda (P0.2).

        Le o `attempt_json` BRUTO, porque o dataclass apaga a diferenca entre
        "campo ausente" (tentativa LEGACY, anterior ao P0.1, que pode ter chegado
        ao POST sem nunca marcar nada) e "campo presente e vazio" (tentativa do
        codigo novo, comprovadamente antes da fronteira):

            chave AUSENTE        -> unknown  (ambiguo; nunca retry)
            chave presente == ""  -> pre_write (seguro: INTERRUPTED -> REVIEW_REACHED)
            chave presente != ""  -> unknown  (fronteira cruzada; reconciliar)

        Fail-closed no resto: Application em SUBMITTING sem exatamente UMA
        tentativa ativa, ou tentativa apontando para intent inexistente, levanta
        em vez de "consertar" escolhendo uma. Corrupcao de exactly-once nao se
        resolve por heuristica.
        """
        with self.connection:
            row = self.connection.execute("SELECT state FROM applications WHERE id=?", (application_id,)).fetchone()
            if row is None:
                raise ApplicationConflict(f"application not found: {application_id}")
            if str(row[0]) != ApplicationState.SUBMITTING.value:
                return self.RECOVERY_NOOP
            attempts = self.connection.execute(
                "SELECT id, intent_id, attempt_json FROM submission_attempts "
                "WHERE application_id=? AND status=?",
                (application_id, ApplicationState.SUBMITTING.value),
            ).fetchall()
            if len(attempts) != 1:
                raise ApplicationConflict(
                    "stranded SUBMITTING requires exactly one active attempt, "
                    f"found {len(attempts)} (refusing to guess)"
                )
            attempt_id, intent_id, raw = attempts[0]
            payload = json.loads(raw)
            intent_row = self.connection.execute("SELECT id FROM submission_intents WHERE id=?", (intent_id,)).fetchone()
            if intent_row is None:
                raise ApplicationConflict(f"attempt {attempt_id} references unknown intent {intent_id}")
            if "write_possible_at" not in payload:
                branch = self.RECOVERY_UNKNOWN
            elif str(payload.get("write_possible_at") or ""):
                branch = self.RECOVERY_UNKNOWN
            else:
                branch = self.RECOVERY_PRE_WRITE

            if branch == self.RECOVERY_PRE_WRITE:
                attempt_status, intent_status = "INTERRUPTED", "INTERRUPTED"
                application_status, event = ApplicationState.REVIEW_REACHED.value, "submission_recovery_pre_write"
            else:
                attempt_status = intent_status = application_status = ApplicationState.SUBMIT_UNKNOWN.value
                event = "submission_recovery_unknown"

            payload["status"] = attempt_status
            payload["completed_at"] = now_iso()
            self.connection.execute(
                "UPDATE submission_attempts SET status=?, attempt_json=?, completed_at=? WHERE id=? AND attempt_json=?",
                (attempt_status, canonical_json(payload), payload["completed_at"], attempt_id, raw),
            )
            self._set_intent_status(intent_id, intent_status)
            self.connection.execute(
                "UPDATE applications SET state=? WHERE id=? AND state=?",
                (application_status, application_id, ApplicationState.SUBMITTING.value),
            )
            self.connection.execute(
                """INSERT INTO application_events
                   (application_id, from_state, to_state, event, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    application_id,
                    ApplicationState.SUBMITTING.value,
                    application_status,
                    event,
                    canonical_json(
                        {
                            "attempt_id": attempt_id,
                            "attempt_status": attempt_status,
                            "branch": branch,
                            "write_possible_at": str(payload.get("write_possible_at", "")),
                            "action": "reconcile_confirmation" if branch == self.RECOVERY_UNKNOWN else "resume",
                        }
                    ),
                    now_iso(),
                ),
            )
        return branch

    def begin_submission_attempt(self, intent: SubmissionIntent, attempt: SubmissionAttempt, event: ApplicationEvent) -> None:
        """Persist the attempt before network I/O and advance state atomically."""
        with self.connection:
            intent_row = self.connection.execute("SELECT status FROM submission_intents WHERE id=?", (intent.id,)).fetchone()
            if not intent_row or str(intent_row[0]) != "AUTHORIZED":
                raise ApplicationConflict(f"submission intent is not authorized: {intent.id}")
            cursor = self.connection.execute(
                """UPDATE applications SET state=?, updated_at=?
                   WHERE id=? AND state=?""",
                (ApplicationState.SUBMITTING.value, event.created_at, intent.application_id, ApplicationState.SUBMIT_AUTHORIZED.value),
            )
            if cursor.rowcount != 1:
                raise ApplicationConflict(f"application is not authorized for submission: {intent.application_id}")
            self.connection.execute(
                "UPDATE submission_intents SET status=?, intent_json=? WHERE id=?",
                (intent.status, canonical_json(intent), intent.id),
            )
            self.connection.execute(
                """INSERT INTO submission_attempts
                   (id, intent_id, application_id, provider, method, origin,
                    path_hash, status, attempt_json, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt.id,
                    attempt.intent_id,
                    attempt.application_id,
                    attempt.provider,
                    attempt.method,
                    attempt.origin,
                    attempt.path_hash,
                    attempt.status,
                    canonical_json(attempt),
                    attempt.started_at,
                    attempt.completed_at,
                ),
            )
            self.connection.execute(
                """INSERT INTO application_events
                   (application_id, from_state, to_state, event, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event.application_id, event.from_state.value, event.to_state.value, event.event, canonical_json(event.payload), event.created_at),
            )

    def complete_submission_attempt(self, attempt: SubmissionAttempt, intent: SubmissionIntent, target: ApplicationState, event: ApplicationEvent) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE applications SET state=?, updated_at=?
                   WHERE id=? AND state=?""",
                (target.value, event.created_at, attempt.application_id, ApplicationState.SUBMITTING.value),
            )
            if cursor.rowcount != 1:
                raise ApplicationConflict(f"submission attempt is no longer active: {attempt.id}")
            self.connection.execute(
                "UPDATE submission_attempts SET status=?, attempt_json=?, completed_at=? WHERE id=?",
                (attempt.status, canonical_json(attempt), attempt.completed_at, attempt.id),
            )
            self.connection.execute(
                "UPDATE submission_intents SET status=?, intent_json=? WHERE id=?",
                (intent.status, canonical_json(intent), intent.id),
            )
            self.connection.execute(
                """INSERT INTO application_events
                   (application_id, from_state, to_state, event, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (event.application_id, event.from_state.value, event.to_state.value, event.event, canonical_json(event.payload), event.created_at),
            )

    def abandon_submission_attempt(self, attempt_id: str, intent_id: str) -> None:
        """Fecha uma tentativa estrandada sem inventar um desfecho.

        Um processo interrompido no meio da submissao deixa a tentativa em
        SUBMITTING para sempre, e o guard de duplicidade passava a recusar
        qualquer nova tentativa. O status registrado e ``INTERRUPTED``: nao
        afirma que falhou nem que foi enviada, apenas que nao houve desfecho.
        O ``attempt_json`` e reescrito com o modelo completo porque e dele que as
        leituras desserializam — mudar so a coluna nao teria efeito nenhum.
        """
        attempt = self.get_submission_attempt(attempt_id)
        if attempt is None:
            return
        attempt.status = "INTERRUPTED"
        attempt.completed_at = now_iso()
        intent = self.get_submission_intent(intent_id)
        with self.connection:
            self.connection.execute(
                "UPDATE submission_attempts SET status=?, attempt_json=?, completed_at=? WHERE id=?",
                (attempt.status, canonical_json(attempt), attempt.completed_at, attempt.id),
            )
            if intent is not None:
                intent.status = "INTERRUPTED"
                self.connection.execute(
                    "UPDATE submission_intents SET status=?, intent_json=? WHERE id=?",
                    (intent.status, canonical_json(intent), intent.id),
                )

    def get_handoff_package(self, package_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT package_json FROM human_handoff_packages WHERE id=?", (package_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list_handoff_packages(self, application_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT package_json FROM human_handoff_packages
               WHERE application_id=? ORDER BY created_at, id""",
            (application_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_handoff_package(self, package: Any, application: Application, event: ApplicationEvent) -> None:
        """Pacote, estado e evento na MESMA transacao.

        E o que impede o estado orfao: ou o humano recebe o pacote E a
        Application esta em handoff, ou nada disso aconteceu. O pacote entra
        antes do UPDATE de estado; se a corrida for perdida, o ``with`` desfaz o
        INSERT tambem.
        """
        data = canonical_json(package)
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO human_handoff_packages
                   (id, application_id, job_id, package_sha256, package_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    package.package_id,
                    package.application_id,
                    package.job_id,
                    package.package_sha256,
                    data,
                    package.created_at,
                ),
            )
            row = self.connection.execute(
                "SELECT package_sha256 FROM human_handoff_packages WHERE id=?", (package.package_id,)
            ).fetchone()
            if row is None or str(row[0]) != package.package_sha256:
                # O id e derivado do conteudo: mesma chave com outro digest so
                # acontece com adulteracao, e nesse caso nada e gravado.
                raise ApplicationConflict(f"handoff package id collision: {package.package_id}")
            cursor = self.connection.execute(
                """UPDATE applications SET state=?, updated_at=?
                   WHERE id=? AND state=?""",
                (application.state.value, application.updated_at, application.id, event.from_state.value),
            )
            if cursor.rowcount != 1:
                raise ApplicationConflict(f"application transition lost race: {application.id}")
            self.connection.execute(
                """INSERT INTO application_events
                   (application_id, from_state, to_state, event, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.application_id,
                    event.from_state.value,
                    event.to_state.value,
                    event.event,
                    canonical_json(event.payload),
                    event.created_at,
                ),
            )

    def save_confirmation_evidence(self, application_id: str, evidence: Any, *, accepted: bool) -> str:
        """Registra a evidencia OBSERVADA, aceita ou nao (JSA-CONF-004).

        O id e derivado do conteudo da evidencia: reconciliar duas vezes o mesmo
        e-mail nao cria duas linhas. `accepted` e monotonico — aceitar e um fato
        historico, e uma reconciliacao posterior nao pode desfaze-lo.
        """
        data = canonical_json(evidence)
        evidence_id = "ev-" + hashlib.sha256(data.encode("utf-8")).hexdigest()[:24]
        with self.connection:
            self.connection.execute(
                """INSERT INTO confirmation_evidence
                   (id, application_id, source, provider, confidence, accepted, evidence_json, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     accepted=MAX(confirmation_evidence.accepted, excluded.accepted)""",
                (
                    evidence_id,
                    application_id,
                    evidence.source.value if hasattr(evidence.source, "value") else str(evidence.source),
                    evidence.provider,
                    float(evidence.confidence),
                    1 if accepted else 0,
                    data,
                    evidence.observed_at,
                    now_iso(),
                ),
            )
            row = self.connection.execute(
                "SELECT evidence_json FROM confirmation_evidence WHERE id=?", (evidence_id,)
            ).fetchone()
            if row is None or str(row[0]) != data:
                raise ApplicationConflict(f"confirmation evidence id collision: {evidence_id}")
        return evidence_id

    def list_confirmation_evidence(self, application_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT evidence_json, accepted FROM confirmation_evidence
               WHERE application_id=? ORDER BY observed_at, id""",
            (application_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = json.loads(row[0])
            record["accepted"] = bool(row[1])
            result.append(record)
        return result

    def save_analysis(self, job_id: str, **values: Any) -> None:
        fields = {key: canonical_json(value) if value is not None else None for key, value in values.items() if key in {"analysis", "fit", "strategy", "resume", "validation"}}
        updated_at = str(values.get("updated_at", ""))
        existing = self.connection.execute("SELECT job_id FROM analyses WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            for key, value in fields.items():
                self.connection.execute(f"UPDATE analyses SET {key}_json=?, updated_at=? WHERE job_id=?", (value, updated_at, job_id))
            self.connection.execute("UPDATE analyses SET updated_at=? WHERE job_id=?", (updated_at, job_id))
        else:
            self.connection.execute("INSERT INTO analyses(job_id,analysis_json,fit_json,strategy_json,resume_json,validation_json,updated_at) VALUES (?,?,?,?,?,?,?)", (job_id, fields.get("analysis"), fields.get("fit"), fields.get("strategy"), fields.get("resume"), fields.get("validation"), updated_at))
        self.connection.commit()

    def update_state(self, job_id: str, state: JobState) -> None:
        self.connection.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state.value, datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id))
        self.connection.commit()

    def record_event(self, job_id: str | None, event: str, fields: dict[str, Any], created_at: str) -> None:
        safe = {key: value for key, value in fields.items() if not any(part in key.lower() for part in ("password", "token", "secret", "cookie", "authorization", "api_key"))}
        self.connection.execute("INSERT INTO events(job_id,event,fields_json,created_at) VALUES (?,?,?,?)", (job_id, event, json.dumps(safe, ensure_ascii=False), created_at))
        self.connection.commit()
