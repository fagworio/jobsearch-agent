from pathlib import Path

from jobsearch_agent.models import Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.sources import canonical_job_key


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

