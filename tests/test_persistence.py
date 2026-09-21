from pathlib import Path

from jobsearch_agent.models import Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.sources import JobSpyAdapter, canonical_job_key


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
