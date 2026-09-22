from pathlib import Path

import yaml
import pytest

from jobsearch_agent.application import ApplicationDomainError, ApplicationService, evaluate_safety_gate, load_application_policy
from jobsearch_agent.models import ApplicationContext, ApplicationField, ApplicationForm, ApplicationPolicy, ApplicationState, Job
from jobsearch_agent.persistence import ApplicationConflict, Database
from jobsearch_agent.profile import load_preferences, load_profile
from jobsearch_agent.qa import AnswerKnowledgeBase, load_answers


ROOT = Path(__file__).parents[1]


def test_application_state_machine_persists_events_and_blocks_invalid_transition(tmp_path: Path):
    db = Database(tmp_path / "applications.db")
    job = Job(id="job-1", source="fixture", external_id="1", company="Acme", title="Engineer", description="Build software")
    db.save_job(job, "source:fixture:1", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    stale = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    with pytest.raises(ApplicationConflict):
        service.transition(stale.id, ApplicationState.REJECTED, "stale_transition", expected_state=stale.state)
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


def test_interruption_states_require_explicit_resume():
    db = Database(":memory:")
    job = Job(id="job-2", source="fixture", external_id="2", company="Acme", title="Engineer", description="Build software")
    db.save_job(job, "source:fixture:2", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.NEEDS_LOGIN, "login_required")
    resumed = service.resume(application.id)
    assert resumed.state == ApplicationState.PREPARING
    assert db.list_application_events(application.id)[-1].event == "application_resumed"
    db.close()


def test_captcha_resume_requires_manual_resolution():
    db = Database(":memory:")
    job = Job(id="job-3", source="fixture", external_id="3", company="Acme", title="Engineer", description="Build software")
    db.save_job(job, "source:fixture:3", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.NEEDS_CAPTCHA, "captcha_required")
    assert service.resume(application.id).state == ApplicationState.PREPARING
    db.close()


def test_ready_to_apply_is_reversible_and_artifact_state_is_resumable():
    db = Database(":memory:")
    job = Job(id="job-reversible", source="fixture", external_id="reversible", company="Acme", title="Engineer", description="Build software")
    db.save_job(job, "source:fixture:reversible", {})
    service = ApplicationService(db)
    application = service.create_for_job(job.id)
    service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
    service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")
    service.transition(application.id, ApplicationState.READY_TO_APPLY, "safety_gate_evaluated")
    service.transition(application.id, ApplicationState.NEEDS_ARTIFACT, "artifact_missing")
    assert service.resume(application.id).state == ApplicationState.PREPARING
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


def test_qa_normalizes_pt_br_and_reads_explicit_work_authorization():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"work_authorization": ["Brazil"]})
    kb = AnswerKnowledgeBase([])
    answer = kb.resolve("Você tem autorização de trabalho no Brasil?", profile, preferences)
    assert answer is not None
    assert answer.answer == "Brazil"
    assert answer.semantic_type == "work_authorization"


def test_field_aware_qa_respects_semantic_type_and_options():
    profile = load_profile(ROOT / "profile/career_profile.yaml")
    preferences = load_preferences(ROOT / "profile/preferences.yaml", {"work_authorization": ["Brazil"]})
    kb = AnswerKnowledgeBase([])
    yes_no = ApplicationField("auth", "Authorized to work in Brazil?", field_type="radio", semantic_type="work_authorization", options=["Yes", "No"], required=True, confidence=1.0)
    countries = ApplicationField("countries", "Countries where you are authorized to work in Brazil", field_type="text", semantic_type="work_authorization", required=True, confidence=1.0)
    company = ApplicationField("company", "Current company name", field_type="text", required=True)
    assert kb.resolve_field(yes_no, profile, preferences).answer == "Yes"
    assert kb.resolve_field(countries, profile, preferences).answer == "Brazil"
    assert kb.resolve_field(company, profile, preferences) is None


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
    assert ready.ready_to_apply is False
    assert ready.requires_review is True
    field = ApplicationField("work_authorization", "Work authorization", required=True)
    context.form = ApplicationForm("form-unknown", fields=[field])
    blocked = evaluate_safety_gate(context)
    assert blocked.decision == ApplicationState.NEEDS_ANSWER
    assert "unknown_answer:work_authorization" in blocked.blockers
    assert any(check["gate"] == "required_answers" for check in blocked.checks)


def test_form_is_required_before_ready_to_apply_even_when_policy_is_auto():
    context = ApplicationContext(
        application_id="a",
        job_id="j",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )
    result = evaluate_safety_gate(context)
    assert result.decision == ApplicationState.READY_FOR_REVIEW
    assert result.ready_to_apply is False
    assert any(check["result"] == "not_analyzed" for check in result.checks if check["gate"] == "form")


def test_known_form_can_be_ready_to_apply_without_authorizing_submit():
    context = ApplicationContext(
        application_id="a",
        job_id="j",
        fit={"blockers": []},
        validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        form=ApplicationForm("form-1", fields=[ApplicationField("name", "Name", semantic_type="full_name", required=True, value="Candidate", confidence=1.0, source="CareerProfile")]),
        policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "manual"}),
    )
    result = evaluate_safety_gate(context)
    assert result.decision == ApplicationState.READY_TO_APPLY
    assert result.ready_to_apply is True
    assert result.requires_review is True


def test_application_policy_fixture_is_loaded():
    policy = load_application_policy(ROOT / "profile/application_policy.yaml")
    assert policy.autonomy["fill_forms"] == "review"
    assert policy.autonomy["submit"] == "manual"
    assert policy.applications_per_day == 20
