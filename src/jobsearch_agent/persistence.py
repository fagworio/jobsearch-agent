"""SQLite persistence with versioned, conservative migrations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import Job, JobState, now_iso
from .serialization import canonical_json
from .sources import JobIdentityCandidate, identity_records


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


MIGRATIONS: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "initial", _migration_001_initial),
    (2, "namespaced_identities_and_duplicate_candidates", _migration_002_identities),
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
