from pathlib import Path

import yaml
import pytest

from jobsearch_agent.application import ApplicationDomainError, ApplicationService, evaluate_safety_gate, load_application_policy
from jobsearch_agent.models import ApplicationContext, ApplicationField, ApplicationPolicy, ApplicationState, Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase, load_answers


ROOT = Path(__file__).parents[1]


def test_application_state_machine_persists_events_and_blocks_invalid_transition(tmp_path: Path):
    db = Database(tmp_path / "applications.db")
    job = Job(id="job-1", source="fixture", external_id="1", company="Acme", title="Engineer", description="Build software")
    db.save_job(job, "source:fixture:1", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")
    with pytest.raises(ApplicationDomainError):
        service.transition(application.id, ApplicationState.DRAFT, "invalid")
    events = db.list_application_events(application.id)
    assert [(event.from_state, event.to_state) for event in events] == [
        (ApplicationState.DRAFT, ApplicationState.PREPARING),
        (ApplicationState.PREPARING, ApplicationState.MATERIALS_READY),
    ]
    assert db.get_application(application.id).state == ApplicationState.MATERIALS_READY
    db.close()


def test_qa_precedence_and_legal_questions_never_infer(tmp_path: Path):
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump({"answers": [{"question": "Are you authorized to work in Brazil?", "answer": "Yes", "approved": True, "supported_by": ["preferences.work_authorization"]}]}), encoding="utf-8")
    kb = AnswerKnowledgeBase(load_answers(path))
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"work_authorization": ["Brazil"]})
    exact = kb.resolve("Are you authorized to work in Brazil?", profile, preferences)
    semantic = kb.resolve("Are you authorized to work in Brazil", profile, preferences)
    legal = kb.resolve("Do you have any criminal convictions?", profile, preferences)
    assert exact and exact.source == "approved_answer"
    assert semantic and semantic.answer == "Yes"
    assert legal is None


def test_safety_gate_is_explainable_and_requires_unknown_answer():
    context = ApplicationContext(
        application_id="a",
        job_id="j",
        fit={"blockers": [], "criteria": [{"criterion": "fit", "result": "match"}]},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        policy=ApplicationPolicy(),
    )
    ready = evaluate_safety_gate(context)
    assert ready.decision == ApplicationState.READY_FOR_REVIEW
    assert ready.ready is True
    field = ApplicationField("work_authorization", "Work authorization", required=True)
    blocked = evaluate_safety_gate(context, [field])
    assert blocked.decision == ApplicationState.NEEDS_ANSWER
    assert "unknown_answer:work_authorization" in blocked.blockers
    assert any(check["gate"] == "required_answers" for check in blocked.checks)


def test_application_policy_fixture_is_loaded():
    policy = load_application_policy(ROOT / "profile/application_policy.yaml")
    assert policy.autonomy["fill_forms"] == "review"
    assert policy.autonomy["submit"] == "manual"
    assert policy.applications_per_day == 20
