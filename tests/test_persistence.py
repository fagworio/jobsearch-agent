import sqlite3
from pathlib import Path

from jobsearch_agent.models import Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.sources import JobSpyAdapter, canonical_job_key, identity_records
from jobsearch_agent.serialization import canonical_json


def test_sqlite_round_trip_and_deduplication(tmp_path: Path):
    db = Database(tmp_path / "jobs.db")
    job = Job(id="new", source="greenhouse", external_id="1", company="Co", title="Role", description="desc")
    first = db.save_job(job, canonical_job_key(job), {"id": 1})
    duplicate = Job(id="different", source="greenhouse", external_id="1", company="Co", title="Role", description="updated")
    second = db.save_job(duplicate, canonical_job_key(duplicate), {"id": 1, "v": 2})
    assert first.id == second.id
    assert len(db.list_jobs()) == 1
    assert db.get_job(first.id).description == "updated"
    db.close()


def test_cross_source_identity_deduplicates_direct_url(tmp_path: Path):
    db = Database(tmp_path / "jobs.db")
    direct = Job(id="direct", source="greenhouse", external_id="123", company="Acme", title="Engineer", description="Build APIs", url="https://boards.greenhouse.io/acme/jobs/123", location="Remote")
    db.save_job(direct, canonical_job_key(direct), direct.raw_payload)
    jobspy = JobSpyAdapter().normalize({"id": "abc", "site": "indeed", "company": "Acme", "title": "Engineer", "description": "Build APIs", "job_url": "https://boards.greenhouse.io/acme/jobs/123", "location": "Remote"})
    saved = db.save_job(jobspy, canonical_job_key(jobspy), jobspy.raw_payload)
    assert saved.id == "direct"
    assert len(db.list_jobs()) == 1
    db.close()


def test_weak_identity_preserves_distinct_jobs_and_records_review_candidate(tmp_path: Path):
    db = Database(tmp_path / "jobs.db")
    first = Job(id="first", source="greenhouse", external_id="1", company="Acme", title="Engineer", description="Build APIs", location="Remote")
    second = Job(id="second", source="lever", external_id="1", company="Acme", title="Engineer", description="Build APIs", location="Remote")
    db.save_job(first, canonical_job_key(first), first.raw_payload)
    db.save_job(second, canonical_job_key(second), second.raw_payload)
    assert len(db.list_jobs()) == 2
    candidates = db.list_duplicate_candidates()
    assert candidates
    assert candidates[0]["status"] == "pending"
    db.close()


def test_legacy_database_migrates_idempotently_without_merging_weak_matches(tmp_path: Path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (1), (2);
        CREATE TABLE jobs (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
          canonical_key TEXT NOT NULL UNIQUE, company TEXT NOT NULL, title TEXT NOT NULL,
          url TEXT, state TEXT NOT NULL, payload_json TEXT NOT NULL, job_json TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE job_identities (identity TEXT PRIMARY KEY, job_id TEXT NOT NULL);
        CREATE TABLE analyses (job_id TEXT PRIMARY KEY, analysis_json TEXT NOT NULL, fit_json TEXT,
          strategy_json TEXT, resume_json TEXT, validation_json TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, event TEXT NOT NULL,
          fields_json TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
    first = Job(id="first", source="greenhouse", external_id="1", company="Acme", title="Engineer", description="Build APIs", location="Remote")
    second = Job(id="second", source="lever", external_id="1", company="Acme", title="Engineer", description="Build APIs", location="Remote")
    for job in (first, second):
        payload = canonical_json(job.raw_payload)
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (job.id, job.source, job.external_id, canonical_job_key(job), job.company, job.title, job.url, job.state.value, payload, canonical_json(job), job.discovered_at, job.discovered_at))
    connection.commit()
    connection.close()

    db = Database(path)
    assert [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3]
    assert len(db.list_jobs()) == 2
    assert db.list_duplicate_candidates()
    db.close()
    db = Database(path)
    assert [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations ORDER BY version")] == [1, 2, 3]
    assert len(db.list_jobs()) == 2
    db.close()


def test_identity_records_mark_url_and_source_as_strong_but_text_as_weak():
    job = Job(id="job", source="greenhouse", external_id="123", company="Acme", title="Engineer", description="Build APIs", location="Remote", url="https://boards.greenhouse.io/acme/jobs/123")
    records = identity_records(job)
    assert {item.identity_type for item in records if item.strength == "strong"} == {"source_external_id", "canonical_url"}
    assert {item.identity_type for item in records if item.strength == "weak"} == {"company_title_location", "description_fingerprint"}
