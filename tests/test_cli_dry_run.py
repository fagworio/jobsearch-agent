from pathlib import Path

from pypdf import PdfWriter

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.config import Settings
from jobsearch_agent.models import ApplicationContext, ApplicationPolicy, ApplicationState, Job, to_dict
from jobsearch_agent.persistence import Database
from jobsearch_agent.pipeline import dry_run_application


ROOT = Path(__file__).parents[1]


def test_dry_run_application_cli_path_uses_local_html_and_stops_before_submit(tmp_path: Path):
    profile = tmp_path / "candidate.yaml"
    profile.write_text(
        (ROOT / "profile/career_profile.yaml").read_text(encoding="utf-8").replace("demo: true", "demo: false").replace('  email: "demo@example.invalid"', '  email: "demo@example.invalid"\n  phone: "+5511999999999"'),
        encoding="utf-8",
    )
    db_path = tmp_path / "jobs.db"
    artifact_root = tmp_path / "artifacts" / "job-dry-run"
    artifact_root.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with (artifact_root / "resume.pdf").open("wb") as handle:
        writer.write(handle)
    db = Database(db_path)
    job = Job(id="job-dry-run", source="linkedin", external_id="dry-run", company="Acme", title="Engineer", description="Build")
    db.save_job(job, "linkedin:dry-run", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "prepare")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "ready")
    context = ApplicationContext(
        application_id=application.id,
        job_id=job.id,
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )
    application.context = to_dict(context)
    db.save_application(application)
    db.close()

    settings = Settings.from_args(
        ROOT,
        db=str(db_path),
        artifacts=str(tmp_path / "artifacts"),
        profile=str(profile),
        facts=str(ROOT / "profile/locked_facts.yaml"),
        preferences=str(ROOT / "profile/preferences.yaml"),
        answers=str(ROOT / "profile/answers.yaml"),
        application_policy=str(ROOT / "profile/application_policy.yaml"),
    )
    result = dry_run_application(
        settings,
        application.id,
        ROOT / "tests/fixtures/linkedin/easy-apply-single.html",
        provider="linkedin",
    )
    assert result["status"] == "STOP_BEFORE_SUBMIT"
    assert result["execution"]["stopped_before_submit"] is True
    assert result["execution"]["submission_attempted"] is False
    assert result["network_access"] == "none"
