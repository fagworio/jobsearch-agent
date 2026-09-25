"""Orquestração da milestone Analyze + Generate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .analysis import analyze_requirements, build_strategy, calculate_fit, detect_language
from .application import ApplicationService, context_from_dict, evaluate_safety_gate, load_application_policy
from .ats import GreenhouseAdapter, adapter_for
from .browser import AuthorizedWrite, DryRunBrowserExecutor, PlaywrightSessionManager
from .challenge_gate import PreSubmitChallengeGate
from .live_view import LiveViewRelay
from .challenges import JobsearchChallengeAdapter
from .config import Settings
from .coordinator import SubmissionCoordinator
from .execution import build_execution_plan
from .greenhouse import GreenhouseSubmissionExecutor
from .providers import (
    apply_url as provider_apply_url,
    board_from_url,
    profile_for,
    provider_for_url,
    submit_destination,
)
from .linkedin.inspector import LinkedInApplyClassification, LinkedInInspector
from .loop import LoopRuntime, PreparedMaterial
from .resolver import GroundedTemplateProvider, LLMAnswerProvider
from .llm import OpenAICompatibleProvider
from .models import ApplicationContext, ApplicationState, CandidatePreferences, Job, JobState, now_iso, to_dict
from .observability import append_event
from .orchestrator import LiveApplicationOrchestrator
from .persistence import Database
from .profile import PROFILE_OPTIONAL_PATHS, PROFILE_REQUIRED_PATHS, load_facts, load_preferences, load_profile, validate_facts as validate_profile_facts, validate_profile_readiness
from .qa import AnswerKnowledgeBase, load_answers, load_rules
from .resume import generate_resume, render_docx, render_pdf_from_docx, render_text, select_fact_ids, validate_ats, validate_facts as validate_resume_facts
from .schemas import validate_contract
from .serialization import canonical_json
from .sources import JOBSPY, canonical_job_key, fetch_payload, normalize_payload
from .submission import (
    build_submission_payload,
    compute_answers_fingerprint,
    submission_destination,
)


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


def discover(
    settings: Settings,
    provider: str,
    boards: list[str],
    *,
    query: str = "",
    location: str = "",
    limit: int = 0,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Discover jobs from public ATS board APIs and persist them.

    Read-only GET against the board's own public JSON endpoint. No credentials,
    no session and no scraping: boards that fail are reported without aborting
    the others.
    """
    from .boards import discover_many

    pairs = [(provider, board) for board in boards]
    jobs, failures = discover_many(
        pairs,
        timeout=timeout or settings.request_timeout,
        query=query,
        location=location,
        limit=limit,
    )
    db = Database(settings.resolve(settings.db_path))
    try:
        persisted = []
        for job in jobs:
            job = db.save_job(job, canonical_job_key(job), job.raw_payload)
            db.record_event(job.id, "job_discovered", {"source": job.source, "title": job.title, "board": job.company}, now_iso())
            persisted.append(job)
    finally:
        db.close()
    append_event(settings.root, "jobs_discovered", source=f"board:{provider}", count=len(persisted))
    return {
        "provider": provider,
        "query": query,
        "location": location,
        "count": len(persisted),
        "failures": failures,
        "jobs": [to_dict(job) for job in persisted],
    }


def _answer_base(settings: Settings) -> AnswerKnowledgeBase:
    """Biblioteca reutilizavel + respostas especificas da vaga.

    `--answers` aponta para o arquivo da vaga e substitui o caminho padrao, o
    que antes descartava a biblioteca e as regras reutilizaveis. As duas fontes
    sao somadas: a da vaga primeiro (vence no casamento exato), a biblioteca
    depois, para que as regras por significado continuem valendo.
    """
    paths = [settings.resolve(settings.answers_path)]
    library = settings.root / "profile/answers.local.yaml"
    if library.is_file() and library not in paths:
        paths.append(library)
    answers = []
    rules = []
    for path in paths:
        answers.extend(load_answers(path))
        rules.extend(load_rules(path))
    return AnswerKnowledgeBase(answers, rules)


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


def precheck_job(settings: Settings, job_id: str) -> dict[str, Any]:
    """Assess job fit and candidate readiness without persisting or opening a browser."""
    db = Database(settings.resolve(settings.db_path))
    try:
        job = db.get_job(job_id)
    finally:
        db.close()
    if not job:
        raise PipelineError(f"job not found: {job_id}")
    profile, _facts = _load_profile_data(settings)
    preferences = profile.candidate_preferences
    analysis = analyze_requirements(job, provider_for(settings))
    fit = calculate_fit(job, analysis, profile)
    description = job.description or ""
    optional_requirements = _explicitly_required_profile_paths(description)
    required_paths = tuple(dict.fromkeys((*PROFILE_REQUIRED_PATHS, *optional_requirements)))
    optional_paths = tuple(path for path in PROFILE_OPTIONAL_PATHS if path not in optional_requirements)
    readiness = validate_profile_readiness(profile, preferences, required_paths, optional_paths)
    authorization_unknown = "work_authorization_unknown" in fit.blockers
    fit_blockers = [item for item in fit.blockers if item != "work_authorization_unknown"]
    fit_status = "UNKNOWN" if authorization_unknown and not fit_blockers else "BLOCKED" if fit_blockers else "MATCH"
    profile_blockers = list(readiness.blockers)
    if authorization_unknown:
        countries = analysis.work_authorization_requirement.countries or ["unspecified"]
        profile_blockers.extend(
            f"work_authorization:{country.title() if country != 'unspecified' else country}"
            for country in countries
        )
    policy_blockers = ["demo_profile"] if profile.demo else []
    ready_for_dry_run = not fit_blockers and not profile_blockers and not policy_blockers
    decision = (
        "DEMO_PROFILE_BLOCKED"
        if policy_blockers
        else "BLOCKED_FIT"
        if fit_blockers
        else "NEEDS_PROFILE_DATA"
        if profile_blockers
        else "READY"
    )
    return {
        "decision": decision,
        "ready_for_dry_run": ready_for_dry_run,
        "browser_started": False,
        "job": {
            "id": job.id,
            "company": job.company,
            "title": job.title,
        },
        "fit": {"status": fit_status, "blockers": fit_blockers, "score": fit.score},
        "profile": {
            "status": "NEEDS_DATA" if profile_blockers else "READY",
            "missing_required": profile_blockers,
            "missing_optional": readiness.missing_optional,
        },
        "policy": {
            "status": "BLOCKED" if policy_blockers else "PASS",
            "blockers": ["DEMO_PROFILE_BLOCKED"] if policy_blockers else [],
            "message": "Demo profile cannot be used for public dry-run fill." if policy_blockers else "",
        },
        "work_authorization_requirement": to_dict(analysis.work_authorization_requirement),
    }


def _explicitly_required_profile_paths(description: str) -> list[str]:
    """Promote optional profile data only when the posting explicitly requires it."""
    fields = {
        "phone": r"phone|telephone",
        "linkedin": r"linkedin",
        "github": r"github",
        "timezone": r"time[ -]?zone|timezone",
        "country": r"country of residence|country",
    }
    requirement = r"required|mandatory|must provide|must include|must submit|is needed"
    paths: list[str] = []
    for key, term in fields.items():
        patterns = (
            rf"\b(?:{requirement})\b.{{0,60}}\b(?:{term})\b",
            rf"\b(?:{term})\b.{{0,60}}\b(?:{requirement})\b",
        )
        if any(re.search(pattern, description, re.IGNORECASE | re.DOTALL) for pattern in patterns):
            path = f"preferences.timezone" if key == "timezone" else f"identity.{key}"
            paths.append(path)
    return paths


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
        if application.state not in {ApplicationState.DRAFT, ApplicationState.PREPARING}:
            return {"application": to_dict(application), "events": [to_dict(event) for event in db.list_application_events(application.id)]}
        if application.state == ApplicationState.DRAFT:
            application = service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
        policy = load_application_policy(settings.resolve(settings.application_policy_path))
        answers = _answer_base(settings)
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
        resume_path = Path(prepared["artifacts"]) / "resume.pdf"
        if resume_path.is_file():
            application.context["resume_sha256"] = hashlib.sha256(resume_path.read_bytes()).hexdigest()
        db.save_application(application)
        service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready", {"resume_valid": bool(prepared["validation"].get("valid"))})
        readiness = evaluate_safety_gate(context)
        application = service.transition(application.id, readiness.decision, "safety_gate_evaluated", {"decision": readiness.decision.value, "blockers": readiness.blockers})
        application.context["readiness"] = to_dict(readiness)
        db.save_application(application)
        return {"application": to_dict(application), "readiness": to_dict(readiness), "events": [to_dict(event) for event in db.list_application_events(application.id)]}
    finally:
        db.close()


def resume_application(settings: Settings, application_id: str) -> dict[str, Any]:
    db = Database(settings.resolve(settings.db_path))
    try:
        service = ApplicationService(db)
        application = service.resume(application_id)
        context = context_from_dict(application.context)
        readiness = evaluate_safety_gate(context)
        application = service.transition(application.id, readiness.decision, "safety_gate_re_evaluated", {"decision": readiness.decision.value, "blockers": readiness.blockers})
        application.context["readiness"] = to_dict(readiness)
        db.save_application(application)
        return {"application": to_dict(application), "readiness": to_dict(readiness), "events": [to_dict(event) for event in db.list_application_events(application.id)]}
    finally:
        db.close()


def dry_run_application(settings: Settings, application_id: str, html_file: str | Path, provider: str = "") -> dict[str, Any]:
    """Inspect and execute a local fill-only dry-run for a persisted Application.

    The input is a caller-provided local HTML snapshot. No browser is opened and
    no network write is possible; the returned status is always bounded by
    ``STOP_BEFORE_SUBMIT`` when execution reaches the fill phase.
    """
    db = Database(settings.resolve(settings.db_path))
    try:
        application = db.get_application(application_id)
        if not application:
            raise PipelineError(f"application not found: {application_id}")
        state_before_dry_run = application.state.value
        if application.state not in {
            ApplicationState.READY_FOR_REVIEW,
            ApplicationState.READY_TO_APPLY,
            ApplicationState.REVIEW_REACHED,
        }:
            return {
                "status": "APPLICATION_STATE_BLOCKED",
                "application_id": application_id,
                "application_state": application.state.value,
                "network_access": "none",
            }
        profile = load_profile(settings.resolve(settings.profile_path))
        if profile.demo:
            return {"status": "DEMO_PROFILE_BLOCKED", "application_id": application_id, "network_access": "none"}
        preferences = load_preferences(settings.resolve(settings.preferences_path), profile.preferences)
        profile.candidate_preferences = preferences
        context = context_from_dict(application.context)
        html = Path(html_file).read_text(encoding="utf-8")
        selected_provider = provider or (db.get_job(application.job_id).source if db.get_job(application.job_id) else "")
        if selected_provider == "linkedin":
            inspected = LinkedInInspector().inspect_html(html, form_id=application_id)
            if inspected.classification != LinkedInApplyClassification.EASY_APPLY or inspected.form is None or inspected.bindings is None:
                return {
                    "status": inspected.classification.value,
                    "application_id": application_id,
                    "auth_state": inspected.auth_state,
                    "warnings": inspected.warnings,
                    "network_access": "none",
                }
            form = inspected.form
            bindings = inspected.bindings
        elif selected_provider == "greenhouse":
            inspected = GreenhouseAdapter().inspect(html, form_id=application_id)
            form = inspected.form
            bindings = inspected.bindings
        else:
            raise PipelineError(f"unsupported dry-run provider: {selected_provider}")
        form.artifact_root = str(settings.resolve(settings.artifacts_dir) / application.job_id)
        default_resume = Path(form.artifact_root) / "resume.pdf"
        for field in form.fields:
            if field.field_type.casefold() == "file" and field.semantic_type == "resume" and default_resume.is_file():
                field.attachment_path = str(default_resume)
        context.form = form
        answers = _answer_base(settings)
        for field in form.fields:
            field.answer = answers.resolve_field(field, profile, preferences)
        readiness = evaluate_safety_gate(context)
        review_ready = readiness.decision.value == "READY_FOR_REVIEW" and not readiness.blockers
        if not readiness.ready_to_apply and not review_ready:
            return {
                "status": readiness.decision.value,
                "application_id": application_id,
                "readiness": readiness,
                "network_access": "none",
            }
        plan = build_execution_plan(context, bindings, allow_review=True)
        # The LinkedIn offline inspector owns the snapshot semantics; passing
        # no second HTML snapshot avoids reinterpreting it through generic ATS
        # adapters while keeping plan/context validation active.
        execution = DryRunBrowserExecutor().execute(context, plan, bindings, None, allow_review=True)
        context.validation["dry_run"] = {
            "form_fingerprint": plan.form_fingerprint,
            "answers_fingerprint": compute_answers_fingerprint(form),
            "status": execution.status,
        }
        db.save_application_form(application_id, form)
        resume_sha256 = application.context.get("resume_sha256", "")
        application.context = to_dict(context)
        if resume_sha256:
            application.context["resume_sha256"] = resume_sha256
        db.save_application(application)
        transition_event = "dry_run_fill_only_no_state_transition"
        if application.state == ApplicationState.READY_FOR_REVIEW:
            transition_event = "dry_run_review_reached"
            application = ApplicationService(db).transition(
                application.id,
                ApplicationState.REVIEW_REACHED,
                "dry_run_review_reached",
                {"network_access": "none", "submission_attempted": False},
            )
        return {
            "status": "STOP_BEFORE_SUBMIT",
            "application_id": application_id,
            "provider": selected_provider,
            "readiness": to_dict(readiness),
            "plan": to_dict(plan),
            "execution": to_dict(execution),
            "state_transition": {
                "from": state_before_dry_run,
                "to": application.state.value,
                "event": transition_event,
            },
            "network_access": "none",
        }
    finally:
        db.close()


def apply_live(
    settings: Settings,
    job_id: str,
    *,
    headless: bool = True,
    allow_advance: bool = True,
    max_cycles: int = 5,
    submit: bool = False,
    submit_timeout: float = 10.0,
    intent_ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Open the job's real page, fill it, upload the resume and stop before submit.

    The browser runs inside the same guarded session used by the dry run: every
    network write is blocked, so this flow can fill and upload but can never
    submit. ``submit=True`` keeps the same browser session open and lets the
    board's own application perform the POST, after an explicitly authorized
    one-shot write; there is no separate HTTP client rebuild.
    """
    prepared = prepare(settings, job_id)
    artifact_dir = Path(prepared["artifacts"])
    resume_path = artifact_dir / "resume.pdf"
    if not prepared["validation"].get("valid"):
        return {
            "status": "MATERIALS_INVALID",
            "job_id": job_id,
            "validation": prepared["validation"],
            "browser_started": False,
        }
    if not resume_path.is_file():
        return {"status": "MISSING_RESUME_ARTIFACT", "job_id": job_id, "browser_started": False}

    db = Database(settings.resolve(settings.db_path))
    try:
        job = db.get_job(job_id)
        if not job:
            raise PipelineError(f"job not found: {job_id}")
        if not job.url:
            raise PipelineError(f"job has no application URL: {job_id}")
        adapter = adapter_for(job.url, "")
        if adapter is None:
            # Sem adapter, mas com provider reconhecido pelo host: recusa
            # explicita em vez de erro generico. O Ashby carrega o formulario
            # por POST na API, entao nem a inspecao dispensa escrita.
            known = provider_for_url(job.url)
            if known:
                known_profile = profile_for(known)
                if known_profile.form_loaded_by_api_write:
                    return {
                        "status": "UNSUPPORTED_PROVIDER",
                        "reason": "INSPECTION_REQUIRES_WRITE",
                        "provider": known,
                        "job_id": job_id,
                        "browser_started": False,
                        "detail": known_profile.notes,
                    }
            raise PipelineError(f"no supported ATS adapter for live apply: {job.url}")
        candidate_profile, _facts = _load_profile_data(settings)
        preferences = candidate_profile.candidate_preferences
        policy = load_application_policy(settings.resolve(settings.application_policy_path))
        answers = _answer_base(settings)

        service = ApplicationService(db)
        application = service.create_for_job(job_id)
        if application.state == ApplicationState.DRAFT:
            application = service.transition(application.id, ApplicationState.PREPARING, "application_preparing")
        if application.state == ApplicationState.PREPARING:
            application = service.transition(application.id, ApplicationState.MATERIALS_READY, "materials_ready")

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
        resume_sha256 = hashlib.sha256(resume_path.read_bytes()).hexdigest()

        upload_writes_used = 0
        provider_profile = profile_for(adapter.provider)
        form_url = provider_apply_url(adapter.provider, job.url)
        # Hosts de recurso vem do perfil do ATS (CDN, fontes) MAIS os do
        # challenge-guard (o widget precisa carregar). Conhecimento anti-bot nao
        # fica no providers.py: um board que troque de anti-bot nao deve exigir
        # edicao no perfil de ATS.
        resource_hosts = (
            set(provider_profile.resource_hosts)
            | set(getattr(adapter, "resource_allowed_hosts", lambda _url: set())(job.url))
            | JobsearchChallengeAdapter().runtime_read_hosts()
        )
        session = PlaywrightSessionManager(
            headless=headless,
            allowed_hosts=adapter.allowed_hosts(job.url),
            allowed_resource_hosts=resource_hosts,
        )
        session.start()
        try:
            if submit:
                # A subida do curriculo e um POST para o storage do board e
                # precisa acontecer durante o preenchimento. Fica limitada a
                # origem de storage, com um unico envio; sem --submit nenhuma
                # escrita e autorizada e o comportamento dry-run se mantem.
                # restrito ao caminho declarado pelo provider: um curinga
                # autorizaria o proprio endpoint de candidatura, que costuma
                # ficar na mesma origem do upload.
                upload_paths = dict(provider_profile.upload_write_paths)
                session.arm_writes([
                    AuthorizedWrite(
                        application_id=application.id,
                        submission_intent_id="",
                        origin=host,
                        path_pattern=upload_paths.get(host, r"^/.*$"),
                        method="POST",
                        max_writes=1,
                    )
                    for host in provider_profile.upload_write_origins
                ])
            orchestrator = LiveApplicationOrchestrator(
                adapter,
                candidate_profile,
                preferences,
                answers,
                max_cycles=max_cycles,
                allow_advance=allow_advance,
                artifact_root=str(artifact_dir),
                default_resume=str(resume_path),
            )
            live = orchestrator.run(session, context, form_url, audit_dir=artifact_dir / "browser")
            if session.network_guard is not None:
                upload_writes_used = session.network_guard.authorized_writes_used

            response: dict[str, Any] = {
                "status": live.status,
                "job_id": job_id,
                "application_id": application.id,
                "provider": live.provider or adapter.provider,
                "url": live.url or form_url,
                "advanced_steps": live.advanced_steps,
                "form_fingerprint": live.form_fingerprint,
                # Telemetria explicita: o guard segue ativo; o que muda entre
                # dry-run e submissao autorizada e a politica de escrita.
                "network_guard_active": True,
                "write_policy": "authorized_one_shot" if submit else "deny_all",
                "upload_writes_used": upload_writes_used,
                "error": live.error,
            }
            if live.form is not None:
                answers_fingerprint = compute_answers_fingerprint(live.form)
                db.save_application_form(application.id, live.form)
                context.form = live.form
                context.validation["dry_run"] = {
                    "form_fingerprint": live.form_fingerprint,
                    "answers_fingerprint": answers_fingerprint,
                    "status": live.status,
                    "source": "live",
                }
                application.context = to_dict(context)
                application.context["resume_sha256"] = resume_sha256
                application = _advance_to_review(service, application, live, context)
                response["answers_fingerprint"] = answers_fingerprint
                response["application_state"] = application.state.value
                if submit:
                    if live.status != "FILLED_REVIEW_REQUIRED":
                        # Nao tenta submeter um formulario incompleto: reporta as
                        # perguntas sem resposta em vez de falhar no intent.
                        response["submission"] = {
                            "status": "NOT_ATTEMPTED",
                            "reason": f"fill nao chegou ao review: {live.status}",
                            "unanswered_required": _unanswered_questions(live.form),
                        }
                    else:
                        # A sessao segue ABERTA: o submit e executado pela propria
                        # aplicacao no browser, com o guard armado para um POST.
                        response["submission"] = _submit_live_in_browser(
                            db,
                            session,
                            application,
                            job,
                            live.form,
                            artifact_dir,
                            provider=adapter.provider,
                            timeout=submit_timeout,
                            ttl_seconds=intent_ttl_seconds,
                            attachments=live.attachments,
                        )
                        response["submission"]["upload_writes_used"] = upload_writes_used
                        response["application_state"] = db.get_application(application.id).state.value
        finally:
            session.close()

        append_event(
            settings.root,
            "application_live_fill",
            job_id=job_id,
            status=live.status,
            advanced_steps=live.advanced_steps,
        )
        return response
    finally:
        db.close()



def _unanswered_questions(form) -> list[dict[str, str]]:
    """Perguntas obrigatorias que ficaram sem resposta, com o texto legivel."""
    if form is None:
        return []
    pending: list[dict[str, str]] = []
    for field in form.fields:
        if not field.required:
            continue
        # O campo de curriculo responde por artefato anexado, nao por valor: sem
        # isto um formulario completo reportava o resume como pendente.
        if field.value not in (None, "") or (field.answer and field.answer.answer):
            continue
        if str(getattr(field, "attachment_path", "") or "").strip():
            continue
        pending.append({"key": field.key, "question": (field.label or "").strip(), "type": field.field_type})
    return pending


def _advance_to_review(service: ApplicationService, application, live, context: ApplicationContext):
    """Record that a live fill reached the human review surface."""
    readiness = live.readiness or evaluate_safety_gate(context)
    application.context["readiness"] = to_dict(readiness)
    service.database.save_application(application)
    if live.status != "FILLED_REVIEW_REQUIRED":
        return application
    if application.state == ApplicationState.MATERIALS_READY:
        application = service.transition(
            application.id,
            ApplicationState.READY_FOR_REVIEW,
            "live_fill_materials_ready",
            {"network_access": "browser_guarded", "submission_attempted": False},
        )
    if application.state == ApplicationState.READY_FOR_REVIEW:
        application = service.transition(
            application.id,
            ApplicationState.REVIEW_REACHED,
            "live_fill_review_reached",
            {"network_access": "browser_guarded", "submission_attempted": False},
        )
    return application


def _submit_live_in_browser(
    db: Database,
    session,
    application,
    job,
    form,
    artifact_dir: Path,
    *,
    provider: str,
    timeout: float,
    ttl_seconds: int,
    attachments: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Autoriza e executa a unica submissao, pela propria aplicacao no browser.

    A Submission Boundary continua no comando: valida a intent, persiste a
    tentativa antes da escrita e arma o guard para um POST. O pedido e feito
    pelo JavaScript do board, que e o unico capaz de produzir os tokens
    efemeros exigidos pelo endpoint.

    ``attachments`` e aceito para compatibilidade de chamada; o upload ja foi
    feito no proprio formulario durante o preenchimento.
    """
    validation = application.context.get("validation", {})
    live = validation.get("dry_run", {})
    form_fingerprint = str(live.get("form_fingerprint", ""))
    answers_fingerprint = str(live.get("answers_fingerprint", ""))
    resume_sha256 = str(application.context.get("resume_sha256", ""))
    if not all((form_fingerprint, answers_fingerprint, resume_sha256)):
        raise PipelineError("live submission requires form, answers and resume fingerprints")
    destination = submission_destination(
        provider,
        job.company,
        job.external_id,
        job.url,
        form_action=str(getattr(form, "action", "") or ""),
    )
    # Uma unica implementacao da sequencia snapshot -> intent -> autorizacao ->
    # POST -> observacao, compartilhada com o ApplicationLoop. Duplicar aqui era
    # o caminho para as duas copias divergirem.
    outcome = SubmissionCoordinator(db, timeout_seconds=timeout).submit(
        application=application,
        job=job,
        form=form,
        session=session,
        provider=provider,
        destination=destination,
        resume_sha256=resume_sha256,
        form_fingerprint=form_fingerprint,
        answers_fingerprint=answers_fingerprint,
        ttl_seconds=ttl_seconds,
    )
    return outcome.to_dict()


def loop_runtime(
    settings: Settings,
    *,
    headless: bool = True,
    allow_advance: bool = True,
    max_cycles: int = 5,
    submission_timeout: float = 45.0,
    captcha_wait: float = 0.0,
    session_factory: Any | None = None,
    policy_factory: Any | None = None,
    adapter_resolver: Any | None = None,
    allow_insecure_destination: bool = False,
) -> LoopRuntime:
    """Monta o runtime de PRODUCAO do `ApplicationLoop` a partir das Settings.

    Fica aqui, e nao no loop, porque este modulo e o que conhece Settings,
    perfis, adapters e sessao. O loop recebe tudo pronto e continua sem saber o
    que e um provider.

    `adapter_resolver`, `session_factory` e `policy_factory` existem para o teste
    de ponta a ponta: o destino e um ATS controlado em loopback, que nao pertence a
    provider nenhum — e o adapter e como o produto IDENTIFICA o ATS naquele
    endereco. Substituir isso e configurar o DESTINO, nunca o material: perfil,
    fatos, curriculo, respostas, snapshot e intent continuam vindo do caminho de
    producao. E a politica injetada segue sendo validada contra a intent em
    `begin_submission`.
    """
    candidate_profile, _facts = _load_profile_data(settings)
    preferences = candidate_profile.candidate_preferences or CandidatePreferences()
    answers = _answer_base(settings)
    provider_profile = profile_for  # alias local apenas para leitura

    def prepare_material(job: Job) -> PreparedMaterial:
        prepared = prepare(settings, job.id)
        artifact_dir = Path(str(prepared["artifacts"]))
        resume = artifact_dir / "resume.pdf"
        if not resume.is_file():
            raise PipelineError(f"missing resume artifact for live apply: {job.id}")
        return PreparedMaterial(
            resume_path=str(resume),
            resume_sha256=hashlib.sha256(resume.read_bytes()).hexdigest(),
            artifact_root=str(artifact_dir),
            validation=dict(prepared.get("validation", {})),
            resume=dict(prepared.get("resume", {}) or {}),
        )

    def resolve_adapter(job: Job) -> Any:
        adapter = adapter_for(job.url, "")
        if adapter is None:
            known = provider_for_url(job.url)
            if known and provider_profile(known).form_loaded_by_api_write:
                raise PipelineError(
                    f"{known}: o formulario e carregado por POST na API "
                    f"({provider_profile(known).notes}); nao suportado ainda"
                )
            raise PipelineError(f"no supported ATS adapter for live apply: {job.url}")
        return adapter

    def open_session(job: Job, adapter: Any) -> Any:
        profile_here = provider_profile(adapter.provider)
        resource_hosts = (
            set(profile_here.resource_hosts)
            | set(getattr(adapter, "resource_allowed_hosts", lambda _url: set())(job.url))
            | JobsearchChallengeAdapter().runtime_read_hosts()
        )
        session = PlaywrightSessionManager(
            headless=headless,
            allowed_hosts=adapter.allowed_hosts(job.url),
            allowed_resource_hosts=resource_hosts,
        )
        session.start()
        return session

    def upload_permits(job: Job, adapter: Any, application_id: str) -> list[Any]:
        """Permissoes para SUBIR o curriculo, montadas aqui porque este modulo e o
        que conhece o provider.

        Um ATS que carrega o formulario por POST na API nao tem upload de arquivo
        por HTTP: nesse caso nao ha o que armar.
        """
        profile_here = provider_profile(adapter.provider)
        upload_paths = dict(profile_here.upload_write_paths)
        return [
            AuthorizedWrite(
                application_id=application_id,
                submission_intent_id="",
                origin=host,
                path_pattern=upload_paths.get(host, r"^/.*$"),
                method="POST",
                max_writes=1,
            )
            for host in profile_here.upload_write_origins
        ]

    def submission_destination(job: Job, adapter: Any, form: Any) -> str:
        """Endereco que recebe o POST, declarado pelo PROVIDER.

        O formulario e o destino coincidem em alguns ATS (Lever, Greenhouse
        moderno) e NAO coincidem em outros (Workable: formulario em
        `/j/<shortcode>/apply`, candidatura em `/api/v1/accounts/.../applications`).
        Quem sabe disso e `providers.submit_destination`; o loop nao presume.
        """
        return submit_destination(
            adapter.provider,
            job.url,
            board_from_url(adapter.provider, job.url),
            str(job.external_id or ""),
            str(getattr(form, "action", "") or ""),
        )

    # Gate de challenge ANTES da escrita: opt-in por
    # `ENABLE_CHALLENGE_RESOLUTION=true`. Desligado (o padrao) o loop nem
    # instancia o gate, e o comportamento e exatamente o de antes.
    # O relay do operador e a janela em que ele age: nascem com o gate, sob a
    # mesma flag. Sem a flag, nenhum dos dois existe e o comportamento e o de
    # antes.
    live_view_relay = LiveViewRelay() if settings.challenge_resolution_enabled else None
    challenge_gate = (
        PreSubmitChallengeGate(relay=live_view_relay)
        if settings.challenge_resolution_enabled
        else None
    )

    return LoopRuntime(
        adapter_for=adapter_resolver or resolve_adapter,
        form_url=lambda job, adapter: provider_apply_url(adapter.provider, job.url),
        upload_permits=upload_permits,
        submission_destination=submission_destination,
        challenge_gate=challenge_gate,
        challenge_wait_seconds=max(float(captcha_wait), 0.0),
        live_view_relay=live_view_relay,
        profile=candidate_profile,
        preferences=preferences,
        answers=answers,
        prepare=prepare_material,
        open_session=session_factory or open_session,
        policy_for=policy_factory,
        answer_provider=_answer_provider(settings),
        max_cycles=max_cycles,
        allow_advance=allow_advance,
        submission_timeout=submission_timeout,
        # Destino http:// so existe em ambiente controlado (loopback), e o padrao
        # segue recusando: uma submissao real exige HTTPS.
        allow_insecure_destination=allow_insecure_destination,
    )


def _answer_provider(settings: Settings):
    """Gerador de resposta discursiva: LLM quando configurado, grounded caso contrario.

    Sem LLM o produto NAO deixa de responder: entra o gerador deterministico, que
    monta a resposta apenas com itens do contexto autorizado e devolve o
    `supported_by` deles. O que ele nunca faz e inventar fato — e o validador
    confere isso de qualquer forma. Melhorar a qualidade da geracao e o JSA-QA-002.
    """
    if settings.llm_base_url and settings.llm_api_key and settings.llm_model:
        return LLMAnswerProvider(
            OpenAICompatibleProvider(
                settings.llm_base_url,
                settings.llm_api_key,
                settings.llm_model,
                timeout=settings.request_timeout,
            )
        )
    return GroundedTemplateProvider()


def run(settings: Settings, payload: dict[str, Any] | None = None, url: str = "", language_override: str | None = None) -> dict[str, Any]:
    if payload is None:
        payload = fetch_payload(url, settings.request_timeout)
    ingested = ingest(settings, payload, url)
    analyzed = analyze(settings, ingested["job_id"], language_override)
    prepared = prepare(settings, ingested["job_id"], language_override)
    return {"job_id": ingested["job_id"], "analysis": analyzed, "prepared": prepared}
