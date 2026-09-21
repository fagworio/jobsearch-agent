"""SQLite persistence with small explicit repositories."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Job, JobState, to_dict
from .serialization import canonical_json
from .sources import identity_candidates


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS jobs (
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
);
CREATE TABLE IF NOT EXISTS job_identities (
  identity TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS analyses (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id),
  analysis_json TEXT NOT NULL,
  fit_json TEXT,
  strategy_json TEXT,
  resume_json TEXT,
  validation_json TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  event TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (1)")
        self.connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (2)")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def save_job(self, job: Job, canonical_key: str, raw_payload: dict[str, Any]) -> Job:
        payload = canonical_json(raw_payload)
        identities = identity_candidates(job)
        existing = self.connection.execute(
            "SELECT id FROM jobs WHERE canonical_key = ?", (canonical_key,)
        ).fetchone()
        if not existing:
            placeholders = ",".join("?" for _ in identities)
            existing = self.connection.execute(
                f"SELECT job_id AS id FROM job_identities WHERE identity IN ({placeholders}) ORDER BY rowid LIMIT 1", identities
            ).fetchone()
        if existing:
            job.id = str(existing[0])
        data = canonical_json(job)
        if existing:
            self.connection.execute(
                "UPDATE jobs SET job_json=?, payload_json=?, state=?, updated_at=? WHERE id=?",
                (data, payload, job.state.value, job.discovered_at, job.id),
            )
        else:
            self.connection.execute(
                "INSERT INTO jobs(id,source,external_id,canonical_key,company,title,url,state,payload_json,job_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.source, job.external_id, canonical_key, job.company, job.title,
                 job.url, job.state.value, payload, data, job.discovered_at, job.discovered_at),
            )
        for identity in identities:
            self.connection.execute("INSERT OR IGNORE INTO job_identities(identity, job_id) VALUES (?,?)", (identity, job.id))
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

    def save_analysis(self, job_id: str, **values: Any) -> None:
        fields = {key: canonical_json(value) if value is not None else None for key, value in values.items() if key in {"analysis", "fit", "strategy", "resume", "validation"}}
        updated_at = str(values.get("updated_at", ""))
        existing = self.connection.execute("SELECT job_id FROM analyses WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            for key, value in fields.items():
                self.connection.execute(f"UPDATE analyses SET {key}_json=?, updated_at=? WHERE job_id=?", (value, updated_at, job_id))
            self.connection.execute("UPDATE analyses SET updated_at=? WHERE job_id=?", (updated_at, job_id))
        else:
            self.connection.execute(
                "INSERT INTO analyses(job_id,analysis_json,fit_json,strategy_json,resume_json,validation_json,updated_at) VALUES (?,?,?,?,?,?,?)",
                (job_id, fields.get("analysis"), fields.get("fit"), fields.get("strategy"), fields.get("resume"), fields.get("validation"), updated_at),
            )
        self.connection.commit()

    def update_state(self, job_id: str, state: JobState) -> None:
        self.connection.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state.value, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"), job_id))
        self.connection.commit()

    def record_event(self, job_id: str | None, event: str, fields: dict[str, Any], created_at: str) -> None:
        safe = {key: value for key, value in fields.items() if not any(part in key.lower() for part in ("password", "token", "secret", "cookie", "authorization", "api_key"))}
        self.connection.execute("INSERT INTO events(job_id,event,fields_json,created_at) VALUES (?,?,?,?)", (job_id, event, json.dumps(safe, ensure_ascii=False), created_at))
        self.connection.commit()
