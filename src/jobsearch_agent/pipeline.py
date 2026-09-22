"""Orquestração da milestone Analyze + Generate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .analysis import analyze_requirements, build_strategy, calculate_fit, detect_language
from .application import ApplicationService, evaluate_safety_gate, load_application_policy
from .config import Settings
from .llm import OpenAICompatibleProvider
from .models import ApplicationContext, ApplicationState, JobState, now_iso, to_dict
from .observability import append_event
from .persistence import Database
from .profile import load_facts, load_preferences, load_profile, validate_facts as validate_profile_facts
from .qa import AnswerKnowledgeBase, load_answers
from .resume import generate_resume, render_docx, render_pdf_from_docx, render_text, select_fact_ids, validate_ats, validate_facts as validate_resume_facts
from .schemas import validate_contract
from .serialization import canonical_json
from .sources import JOBSPY, canonical_job_key, fetch_payload, normalize_payload


class PipelineError(RuntimeError):
    pass


def provider_for(settings: Settings):
    if settings.llm_base_url and settings.llm_api_key and settings.llm_model:
        return OpenAICompatibleProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.request_timeout)
    return None


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ingest(settings: Settings, payload: dict[str, Any], url: str = "") -> dict[str, Any]:
    job = normalize_payload(payload, url)
    db = Database(settings.resolve(settings.db_path))
    try:
        job = db.save_job(job, canonical_job_key(job), payload)
        db.record_event(job.id, "job_ingested", {"source": job.source, "title": job.title}, now_iso())
    finally:
        db.close()
    append_event(settings.root, "job_ingested", job_id=job.id, source=job.source, title=job.title)
    return {"job": to_dict(job), "job_id": job.id}


def ingest_url(settings: Settings, url: str) -> dict[str, Any]:
    return ingest(settings, fetch_payload(url, settings.request_timeout), url)


def search(settings: Settings, query: str, *, sites: list[str] | None = None, location: str = "", results_wanted: int = 20) -> dict[str, Any]:
    jobs = JOBSPY.search(query, sites=sites, location=location, results_wanted=results_wanted)
    db = Database(settings.resolve(settings.db_path))
    try:
        persisted = []
        for job in jobs:
            job = db.save_job(job, canonical_job_key(job), job.raw_payload)
            db.record_event(job.id, "job_discovered", {"source": job.source, "title": job.title}, now_iso())
            persisted.append(job)
    finally:
        db.close()
    append_event(settings.root, "jobs_discovered", query=query, count=len(persisted), source="jobspy")
    return {"query": query, "source": "jobspy", "count": len(persisted), "jobs": [to_dict(job) for job in persisted]}


def _load_profile_data(settings: Settings):
    profile = load_profile(settings.resolve(settings.profile_path))
    facts = load_facts(settings.resolve(settings.facts_path))
    preferences = load_preferences(settings.resolve(settings.preferences_path), profile.preferences)
    profile.candidate_preferences = preferences
    if settings.real_profile and profile.demo:
        raise PipelineError("demo profile cannot be used with --real-profile")
    errors = validate_profile_facts(profile, facts)
    if errors:
        raise PipelineError("invalid career profile: " + "; ".join(errors))
    return profile, facts


def analyze(settings: Settings, job_id: str, language_override: str | None = None) -> dict[str, Any]:
    db = Database(settings.resolve(settings.db_path))
    try:
        job = db.get_job(job_id)
        if not job:
            raise PipelineError(f"job not found: {job_id}")
        profile, facts = _load_profile_data(settings)
        analysis = analyze_requirements(job, provider_for(settings))
        if language_override:
            analysis.language = detect_language("", language_override)
        fit = calculate_fit(job, analysis, profile)
        job.language = analysis.language.locale
        job.requirements = list(analysis.required_skills)
        job.preferred_requirements = list(analysis.preferred_skills)
        job.state = JobState.SCORED
        validate_contract("job", job)
        validate_contract("analysis", analysis)
        validate_contract("fit", fit)
        db.save_job(job, canonical_job_key(job), job.raw_payload)
        db.save_analysis(job.id, analysis=analysis, fit=fit, updated_at=now_iso())
        db.record_event(job.id, "job_analyzed", {"score": fit.score, "language": analysis.language.locale}, now_iso())
        append_event(settings.root, "job_analyzed", job_id=job.id, score=fit.score, language=analysis.language.locale)
        return {"job": to_dict(job), "analysis": to_dict(analysis), "fit": to_dict(fit)}
    finally:
        db.close()


def prepare(settings: Settings, job_id: str, language_override: str | None = None) -> dict[str, Any]:
    db = Database(settings.resolve(settings.db_path))
    try:
        job = db.get_job(job_id)
        if not job:
            raise PipelineError(f"job not found: {job_id}")
        profile, facts = _load_profile_data(settings)
        analysis = analyze_requirements(job, provider_for(settings))
        if language_override:
            analysis.language = detect_language("", language_override)
        fit = calculate_fit(job, analysis, profile)
        job.language = analysis.language.locale
        job.requirements = list(analysis.required_skills)
        job.preferred_requirements = list(analysis.preferred_skills)
        strategy = build_strategy(job, analysis, fit, profile)
        selected = select_fact_ids(job, strategy, profile, facts)
        rewrite_fallbacks: list[dict[str, str]] = []
        resume = generate_resume(job, strategy, profile, facts, selected, provider_for(settings), rewrite_fallbacks)
        validate_contract("job", job)
        validate_contract("analysis", analysis)
        validate_contract("fit", fit)
        validate_contract("strategy", strategy)
        validate_contract("resume", resume)
        fact_validation = validate_resume_facts(resume, facts)
        ats_validation = validate_ats(resume)
        all_valid = fact_validation.valid and ats_validation.valid
        report = {"facts": to_dict(fact_validation), "ats": to_dict(ats_validation), "valid": all_valid, "state": JobState.RESUME_READY.value if all_valid else JobState.NEEDS_HUMAN.value}
        artifact_dir = settings.resolve(settings.artifacts_dir) / job.id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for filename, value in (("job.json", job), ("analysis.json", analysis), ("fit.json", fit), ("resume-strategy.json", strategy), ("resume.json", resume), ("validation.json", report)):
            _write_json(artifact_dir / filename, value)
        (artifact_dir / "resume.txt").write_text(render_text(resume), encoding="utf-8")
        docx_path = render_docx(resume, artifact_dir / "resume.docx")
        pdf_error = ""
        try:
            render_pdf_from_docx(docx_path, artifact_dir / "resume.pdf")
        except RuntimeError as exc:
            pdf_error = str(exc)
            report["valid"] = False
            report["state"] = JobState.NEEDS_HUMAN.value
            report.setdefault("errors", []).append(pdf_error)
            _write_json(artifact_dir / "validation.json", report)
        job.state = JobState.RESUME_READY if report["valid"] else JobState.NEEDS_HUMAN
        db.save_job(job, canonical_job_key(job), job.raw_payload)
        db.save_analysis(job.id, analysis=analysis, fit=fit, strategy=strategy, resume=resume, validation=report, updated_at=now_iso())
        db.record_event(job.id, "resume_prepared", {"valid": report["valid"], "state": job.state.value}, now_iso())
        for fallback in rewrite_fallbacks:
            db.record_event(job.id, "resume_rewrite_fallback", fallback, now_iso())
        append_event(settings.root, "resume_prepared", job_id=job.id, valid=report["valid"], state=job.state.value)
        return {"job_id": job.id, "fit": to_dict(fit), "strategy": to_dict(strategy), "resume": to_dict(resume), "validation": report, "artifacts": str(artifact_dir), "pdf_error": pdf_error}
    finally:
        db.close()


def prepare_application(settings: Settings, job_id: str, language_override: str | None = None) -> dict[str, Any]:
    """Create/resume an Application without opening a browser or submitting."""
    prepared = prepare(settings, job_id, language_override)
    db = Database(settings.resolve(settings.db_path))
    try:
        service = ApplicationService(db)
        application = service.create_for_job(job_id)
        if application.state in {ApplicationState.READY_FOR_REVIEW, ApplicationState.READY_TO_APPLY, ApplicationState.REJECTED, ApplicationState.POLICY_BLOCKED}:
            return {"application": to_dict(application), "events": [to_dict(event) for event in db.list_application_events(application.id)]}
        if application.state == ApplicationState.DRAFT:
            application = service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
        policy = load_application_policy(settings.resolve(settings.application_policy_path))
        answers = AnswerKnowledgeBase(load_answers(settings.resolve(settings.answers_path)))
        context = ApplicationContext(
            application_id=application.id,
            job_id=job_id,
            fit=prepared["fit"],
            resume=prepared["resume"],
            validation=prepared["validation"],
            answers=[],
            form=None,
            policy=policy,
        )
        # Loading the KB here intentionally does not infer missing questions;
        # form fields are populated only by a future ATS adapter.
        _ = answers
        application.context = to_dict(context)
        db.save_application(application)
        service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready", {"resume_valid": bool(prepared["validation"].get("valid"))})
        readiness = evaluate_safety_gate(context)
        application = service.transition(application.id, readiness.decision, "safety_gate_evaluated", {"decision": readiness.decision.value, "blockers": readiness.blockers})
        application.context["readiness"] = to_dict(readiness)
        db.save_application(application)
        return {"application": to_dict(application), "readiness": to_dict(readiness), "events": [to_dict(event) for event in db.list_application_events(application.id)]}
    finally:
        db.close()


def run(settings: Settings, payload: dict[str, Any] | None = None, url: str = "", language_override: str | None = None) -> dict[str, Any]:
    if payload is None:
        payload = fetch_payload(url, settings.request_timeout)
    ingested = ingest(settings, payload, url)
    analyzed = analyze(settings, ingested["job_id"], language_override)
    prepared = prepare(settings, ingested["job_id"], language_override)
    return {"job_id": ingested["job_id"], "analysis": analyzed, "prepared": prepared}
