"""Loop único de discovery, shortlist, pipeline e execução controlada.

O módulo junta as etapas que antes precisavam ser chamadas manualmente, mas
mantém os limites e a política como fronteiras explícitas. O modo padrão é
somente planejamento; execução real só é liberada quando a política local
declara ``submit: auto``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .answers import AnswerLibrary
from .auto_apply import AutoApplyItem, AutoApplyReport, _write_report, run_auto_apply
from .browser import NativeMessagingClient
from .discovery import (
    DiscoveryMatrix,
    DiscoverySearchRun,
    GreenhouseDiscoveryAdapter,
    SearchBudget,
    SearchCursor,
    build_pipeline,
    build_query_matrix,
    load_match_profile,
    load_policy,
    match_matrix,
    plan_batch,
    rank_shortlist,
    save_matrix,
    save_pipeline,
    save_shortlist,
)
from .discovery.greenhouse import DiscoveryInspectionError
from .facts import FactStore
from .version import ENGINE_VERSION


@dataclass(frozen=True)
class AutonomousRunReport:
    run_id: str
    mode: str
    state: str
    matrix_path: str
    shortlist_path: str
    pipeline_path: str
    state_path: str
    queries_processed: int
    jobs_discovered: int
    jobs_unique: int
    target_submissions: int
    approved: int
    approved_unapplied: int
    submitted: int
    target_reached: bool
    counts: dict[str, int]
    pending_questions_count: int = 0
    search_errors: tuple[str, ...] = ()
    auto_apply: AutoApplyReport | None = None
    reason: str = ""
    campaign_path: str = ""
    submitted_this_run: int = 0
    submitted_total: int = 0
    remaining_target: int = 0
    submit_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "mode": self.mode,
            "state": self.state,
            "reason": self.reason,
            "artifacts": {
                "matrix": self.matrix_path,
                "shortlist": self.shortlist_path,
                "pipeline": self.pipeline_path,
                "state": self.state_path,
            },
            "queries_processed": self.queries_processed,
            "jobs_discovered": self.jobs_discovered,
            "jobs_unique": self.jobs_unique,
            "target_submissions": self.target_submissions,
            "approved": self.approved,
            "approved_unapplied": self.approved_unapplied,
            "submitted": self.submitted,
            "submitted_this_run": self.submitted_this_run,
            "submitted_total": self.submitted_total,
            "remaining_target": self.remaining_target,
            "submit_attempts": self.submit_attempts,
            "target_reached": self.target_reached,
            "counts": dict(self.counts),
            "pending_questions_count": self.pending_questions_count,
            "search_errors": list(self.search_errors),
        }
        if self.campaign_path:
            payload["artifacts"]["campaign"] = self.campaign_path
        if self.auto_apply is not None:
            payload["auto_apply"] = self.auto_apply.to_dict()
        return payload


def _write_state(path: str | Path, payload: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination


def _load_campaign(
    path: str | Path,
    *,
    target_total: int,
    state_path: str | Path,
    marker_store: str | Path,
) -> dict[str, Any]:
    """Load progress without counting unrelated historical submissions.

    The first campaign is bootstrapped from the previous autonomous report,
    not from every marker in ``data/v2-submissions``. This preserves the
    meaning of a campaign target when the account already contains older
    applications.
    """

    destination = Path(path)
    if destination.exists():
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                ids = payload.get("submitted_job_ids")
                campaign = {
                    "campaign_id": str(payload.get("campaign_id") or uuid4().hex),
                    "target_total": int(payload.get("target_total", target_total)),
                    "submitted_job_ids": sorted({str(item) for item in ids or [] if str(item)}),
                    "submit_attempts_total": int(payload.get("submit_attempts_total", 0)),
                    "started_at": str(payload.get("started_at") or datetime.now(timezone.utc).isoformat()),
                }
                return _reconcile_recent_markers(campaign, marker_store)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    submitted_ids: set[str] = set()
    previous = Path(state_path)
    if previous.exists():
        try:
            payload = json.loads(previous.read_text(encoding="utf-8"))
            items = ((payload.get("auto_apply") or {}).get("items") or [])
            submitted_ids.update(
                str(item.get("job_id"))
                for item in items
                if isinstance(item, dict) and item.get("state") == "SUBMITTED" and item.get("job_id")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    return {
        "campaign_id": uuid4().hex,
        "target_total": target_total,
        "submitted_job_ids": sorted(submitted_ids),
        "submit_attempts_total": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _reconcile_recent_markers(campaign: dict[str, Any], marker_store: str | Path) -> dict[str, Any]:
    """Recover confirmed writes if the process stopped before campaign save."""

    started_at = str(campaign.get("started_at") or "")
    ids = {str(item) for item in campaign.get("submitted_job_ids", []) if str(item)}
    if not started_at:
        return campaign
    try:
        marker_paths = Path(marker_store).glob("*.json")
        for path in marker_paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("outcome") != "SUBMITTED":
                continue
            if str(payload.get("write_possible_at") or "") < started_at:
                continue
            job_id = str(payload.get("job_id") or "")
            if job_id:
                ids.add(job_id)
    except OSError:
        return campaign
    campaign["submitted_job_ids"] = sorted(ids)
    return campaign


def _save_campaign(path: str | Path, campaign: dict[str, Any]) -> Path:
    payload = dict(campaign)
    payload["submitted_job_ids"] = sorted({str(item) for item in payload.get("submitted_job_ids", []) if str(item)})
    payload["submitted_total"] = len(payload["submitted_job_ids"])
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return _write_state(path, payload)


def _load_form_profile(path: str | Path) -> dict[str, str]:
    """Mapeia somente identidade explícita do Career Profile para o formulário."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    identity = payload.get("identity") if isinstance(payload, dict) else {}
    if not isinstance(identity, dict):
        return {}
    values: dict[str, str] = {}
    aliases = {
        "first_name": ("First Name",),
        "last_name": ("Last Name",),
        "name": ("Name",),
        "email": ("Email",),
        "phone": ("Phone", "Mobile", "Telephone"),
        "country": ("Country", "Country of residence"),
        "current_location": ("Location", "Location*", "Current Location"),
        "location": ("Location", "Location*", "Current Location"),
        "linkedin": ("LinkedIn Profile", "LinkedIn Profile URL"),
        "github": ("GitHub", "GitHub Profile"),
        "website": ("Website", "Portfolio", "Portfolio URL"),
    }
    for key, labels in aliases.items():
        value = identity.get(key)
        if isinstance(value, str) and value.strip():
            for label in labels:
                values[label] = value
    education = payload.get("education")
    if isinstance(education, list) and education and isinstance(education[0], dict):
        first_education = education[0]
        institution = first_education.get("institution")
        credential = first_education.get("credential")
        if isinstance(institution, str) and institution.strip():
            values["School"] = institution
            values["School*"] = institution
        if isinstance(credential, dict):
            credential = credential.get("en-US") or credential.get("pt-BR")
        if isinstance(credential, str) and credential.strip():
            values["Degree"] = credential
            values["Degree*"] = credential
        for key, label in (("start_date", "Start date year"), ("end_date", "End date year")):
            value = first_education.get(key)
            if isinstance(value, str) and value[:4].isdigit():
                values[label] = value[:4]
    return values


def _facts_from_profile(path: str | Path, facts: FactStore | None) -> FactStore | None:
    """Acrescenta somente valores literais do Career Profile ao FactStore."""

    if facts is None or not isinstance(facts, FactStore):
        return None
    source = Path(path)
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return facts
    preferences_path = source.with_name("preferences.local.yaml")
    preferences = yaml.safe_load(preferences_path.read_text(encoding="utf-8")) if preferences_path.exists() else {}
    if not isinstance(preferences, dict):
        preferences = {}
    result = FactStore(facts.as_dict())
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
    skills = payload.get("skills") if isinstance(payload.get("skills"), dict) else {}
    explicit: dict[str, str] = {}
    for key, fact_id in (
        ("country", "identity.country"),
        ("current_location", "identity.location"),
        ("location", "identity.location"),
        ("linkedin", "identity.linkedin"),
    ):
        value = identity.get(key) if isinstance(identity, dict) else None
        if isinstance(value, str) and value.strip():
            explicit[fact_id] = value
    notice_period = payload.get("notice_period", preferences.get("notice_period"))
    if isinstance(notice_period, str) and notice_period.strip():
        explicit["employment.notice_period"] = notice_period
    sponsorship = payload.get("requires_sponsorship", preferences.get("requires_sponsorship"))
    if isinstance(sponsorship, (bool, str)):
        value = str(sponsorship).casefold()
        if value in {"true", "yes", "sim"}:
            explicit["employment.sponsorship"] = "Yes"
        elif value in {"false", "no", "nao", "não"}:
            explicit["employment.sponsorship"] = "No"
    for skill, fact_id in (
        ("wordpress", "experience.wordpress_years"),
        ("shopify", "experience.shopify_years"),
    ):
        data = skills.get(skill) if isinstance(skills, dict) else None
        if isinstance(data, dict) and isinstance(data.get("years"), (str, int, float)):
            explicit[fact_id] = str(data["years"])
    for fact_id, value in explicit.items():
        if result.get(fact_id) is None:
            result.remember(fact_id, value, source="career_profile")
    return result


def run_autonomous(
    *,
    mode: str = "policy",
    profile_path: str = "profile/career_profile.local.yaml",
    matrix_path: str | Path = "data/v2-agent/matrix.json",
    shortlist_path: str | Path = "data/v2-agent/shortlist.json",
    pipeline_path: str | Path = "data/v2-agent/pipeline.json",
    state_path: str | Path = "data/v2-agent/state.json",
    policy_path: str = "profile/application_policy.yaml",
    target_submissions: int = 3,
    target_ready_jobs: int | None = None,
    max_queries: int = 30,
    max_jobs_inspected: int = 250,
    max_pages: int = 40,
    answers: AnswerLibrary | None = None,
    facts: FactStore | None = None,
    resume: str = "",
    resume_profile: str = "",
    resume_store: str = "data/v2-resumes",
    marker_store: str = "data/v2-submissions",
    report_store: str | Path = "data/v2-auto/auto-apply.json",
    campaign_path: str | Path | None = None,
    max_jobs: int = 5,
    max_submits: int = 3,
    max_submit_attempts: int | None = None,
    max_failures: int = 2,
    human_wait_ms: int = 0,
) -> AutonomousRunReport:
    if mode not in {"policy", "plan", "auto-apply"}:
        raise ValueError("mode must be policy, plan or auto-apply")
    if target_ready_jobs is not None:
        target_submissions = target_ready_jobs
    if target_submissions < 0:
        raise ValueError("target_submissions must be non-negative")
    effective_profile = resume_profile or profile_path
    policy = load_policy(policy_path)
    greenhouse_policy = ((policy.get("providers") or {}).get("greenhouse") or {})
    autonomy_policy = policy.get("autonomy") or {}
    policy_allows_execution = all(
        value == "auto"
        for value in (
            autonomy_policy.get("search"),
            autonomy_policy.get("analyze"),
            autonomy_policy.get("generate_resume"),
            autonomy_policy.get("answer_known_questions"),
            autonomy_policy.get("fill_forms"),
            autonomy_policy.get("submit"),
            greenhouse_policy.get("fill_forms"),
            greenhouse_policy.get("submit"),
        )
    )
    execution_enabled = mode == "auto-apply" or (mode == "policy" and policy_allows_execution)
    if execution_enabled and not policy_allows_execution:
        execution_enabled = False
    configuration_error = execution_enabled and (answers is None or facts is None)
    if configuration_error:
        execution_enabled = False
    effective_campaign_path = campaign_path or Path(state_path).with_name("campaign.json")
    campaign = _load_campaign(
        effective_campaign_path,
        target_total=target_submissions,
        state_path=state_path,
        marker_store=marker_store,
    ) if execution_enabled else {
        "campaign_id": "",
        "target_total": target_submissions,
        "submitted_job_ids": [],
        "submit_attempts_total": 0,
    }
    target_total = int(campaign.get("target_total", target_submissions))
    submitted_ids = set(str(item) for item in campaign.get("submitted_job_ids", []))
    submitted_before = len(submitted_ids)
    remaining_target = max(target_total - submitted_before, 0)
    # In execution mode we cannot stop when the first target-sized shortlist is
    # found: a blocker must be replaceable by a later search result.
    search_target = max_jobs_inspected + 1 if execution_enabled else target_submissions
    budget = SearchBudget(search_target, max_queries, max_jobs_inspected, max_pages)
    budget.validate()
    run_id = uuid4().hex
    adapter = GreenhouseDiscoveryAdapter()
    runs: list[DiscoverySearchRun] = []
    errors: list[str] = []
    cursor = SearchCursor()
    processed_ids: set[str] = set()
    form_profile = _load_form_profile(effective_profile)
    effective_facts = _facts_from_profile(effective_profile, facts)
    applied_items: list[AutoApplyItem] = []
    jobs_processed = 0
    submit_attempts = 0
    submitted = 0
    stop_unknown = False
    detail_by_job_id: dict[str, str] = {}
    detail_inspected: set[str] = set()

    def enrich_runs() -> None:
        """Attach read-only job-page descriptions to all matching occurrences."""

        for index, run in enumerate(runs):
            jobs = tuple(
                replace(job, description=detail_by_job_id[job.job_id])
                if job.job_id in detail_by_job_id else job
                for job in run.results.jobs
            )
            if jobs != run.results.jobs:
                runs[index] = replace(run, results=replace(run.results, jobs=jobs))

    with NativeMessagingClient() as browser:
        try:
            initial = adapter.inspect(browser.inspect_discovery_results())
        except DiscoveryInspectionError as exc:
            if "page type" not in str(exc):
                raise
            # A full unattended run may start on the dashboard or a job page.
            # Open a real search before reading work_type; do not require an
            # operator to prepare the tab manually.
            initial = adapter.inspect(browser.discover_query("frontend", ["remote"]))
    work_type = initial.work_type or ("remote",)
    attempt_budget = max_submit_attempts if max_submit_attempts is not None else max_submits
    success_budget = min(max_submits, remaining_target)
    for item in build_query_matrix():
        if stop_unknown or jobs_processed >= max_jobs or submit_attempts >= attempt_budget or submitted >= success_budget:
            break
        if not cursor.should_continue(budget):
            break
        try:
            # The bridge permits one backend connection at a time. Close the
            # discovery connection before auto-apply opens its own connection.
            with NativeMessagingClient() as browser:
                snapshot = browser.discover_query(item.query, list(work_type))
            result = adapter.inspect(snapshot)
            runs.append(DiscoverySearchRun(item.family, item.query, result))
            partial = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
            matched = match_matrix(partial, load_match_profile(profile_path))
            shortlist = rank_shortlist(matched)

            # Cards frequently show only ``Remote``. Before rejecting a
            # technical candidate for unknown geography, inspect its opened
            # job description read-only. This is the final-fit step; it does
            # not open an application form or submit anything.
            detail_candidates = [
                entry for entry in shortlist.entries
                if entry.selection == "REVIEW"
                and entry.match_score >= 30.0
                and entry.geography_status == "UNKNOWN"
                and not entry.applied
                and entry.job_id not in detail_inspected
            ][:3]
            for entry in detail_candidates:
                detail_inspected.add(entry.job_id)
                try:
                    with NativeMessagingClient() as browser:
                        context = browser.open_job(entry.job_id, entry.url)
                        tab_id = context.get("tab_id")
                        if not isinstance(tab_id, int):
                            raise RuntimeError("OPEN_JOB did not return a valid tab_id")
                        details = browser.inspect_job_details(tab_id=tab_id, job_id=entry.job_id)
                    description = details.get("description")
                    if not isinstance(description, str) or not description.strip():
                        raise RuntimeError("job detail description was not observed")
                    detail_by_job_id[entry.job_id] = description
                except Exception as exc:  # noqa: BLE001 - one detail must not stop discovery
                    errors.append(f"{entry.job_id}: job detail inspection failed: {exc}")
            if detail_candidates:
                enrich_runs()
                partial = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
                matched = match_matrix(partial, load_match_profile(profile_path))
                shortlist = rank_shortlist(matched)
            ready_count = sum(
                1
                for entry in shortlist.entries
                if entry.selection == "APPROVED" and not entry.applied
            )
            cursor = SearchCursor(
                queries_processed=len(runs),
                jobs_inspected=partial.raw_job_count,
                pages=len(runs),
                ready_jobs=ready_count,
            )
            if execution_enabled:
                candidate_manifest = build_pipeline(
                    shortlist.to_dict(),
                    submission_store=marker_store,
                    current_engine_version=ENGINE_VERSION,
                )
                candidates = tuple(item for item in candidate_manifest.items if item.job_id not in processed_ids)
                batch_size = min(max_jobs - jobs_processed, len(candidates))
                attempts_left = attempt_budget - submit_attempts
                successes_left = success_budget - submitted
                if batch_size > 0 and attempts_left > 0 and successes_left > 0:
                    batch_manifest = type(candidate_manifest)(candidate_manifest.source, candidates[:batch_size])
                    batch_report = run_auto_apply(
                        batch_manifest,
                        resume=resume,
                        resume_profile=effective_profile,
                        resume_store=resume_store,
                        library=answers,
                        facts=effective_facts,
                        profile=form_profile,
                        marker_store=marker_store,
                        report_store=report_store,
                        max_jobs=batch_size,
                        max_submits=successes_left,
                        max_submit_attempts=attempts_left,
                        max_failures=max_failures,
                        human_wait_ms=human_wait_ms,
                    )
                    applied_items.extend(batch_report.items)
                    processed_ids.update(item.job_id for item in batch_report.items)
                    jobs_processed += len(batch_report.items)
                    submit_attempts += batch_report.submit_attempts
                    newly_submitted = [item.job_id for item in batch_report.items if item.state == "SUBMITTED"]
                    submitted += len(newly_submitted)
                    submitted_ids.update(newly_submitted)
                    stop_unknown = any(item.state == "SUBMIT_UNKNOWN" for item in batch_report.items)
                    if execution_enabled:
                        campaign["submitted_job_ids"] = sorted(submitted_ids)
                        campaign["submit_attempts_total"] = int(campaign.get("submit_attempts_total", 0)) + sum(
                            int((item.submit or {}).get("submission_writes", 0))
                            for item in batch_report.items
                        )
                        _save_campaign(effective_campaign_path, campaign)
                    if submitted >= remaining_target:
                        break
        except Exception as exc:  # noqa: BLE001 - uma consulta não bloqueia o lote
            errors.append(f"{item.query}: {exc}")
            cursor = SearchCursor(
                queries_processed=cursor.queries_processed + 1,
                jobs_inspected=cursor.jobs_inspected,
                pages=cursor.pages + 1,
                ready_jobs=cursor.ready_jobs,
            )

    matrix = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
    matched = match_matrix(matrix, load_match_profile(profile_path))
    saved_matrix = save_matrix(matched, matrix_path)
    shortlist = rank_shortlist(matched, source=str(saved_matrix))
    saved_shortlist = save_shortlist(shortlist, shortlist_path)
    manifest = build_pipeline(
        shortlist.to_dict(),
        submission_store=marker_store,
        current_engine_version=ENGINE_VERSION,
    )
    saved_pipeline = save_pipeline(manifest, pipeline_path)
    approved_unapplied = sum(1 for item in shortlist.entries if item.selection == "APPROVED" and not item.applied)
    # ``approved`` is a shortlist metric, not the post-marker pipeline size.
    # An old SUBMIT_FAILED marker must not make an approved job disappear from
    # the operational report while it is being retried.
    approved = approved_unapplied
    counts: dict[str, int] = {}
    for item in applied_items:
        counts[item.state] = counts.get(item.state, 0) + 1
    auto_report = None
    if execution_enabled:
        auto_report = AutoApplyReport(
            manifest.source,
            "auto-apply",
            max_jobs,
            max_submits,
            max_failures,
            1,
            tuple(applied_items),
            submit_attempts,
        )
        _write_report(auto_report, report_store)
        submitted_total = len(submitted_ids)
        state = "COMPLETED" if submitted_total >= target_total else ("SUBMIT_UNKNOWN" if stop_unknown else "TARGET_NOT_REACHED")
        reason = "" if state == "COMPLETED" else "search or application budgets exhausted before campaign target"
    elif mode == "auto-apply" and not policy_allows_execution:
        state = "POLICY_BLOCKED"
        reason = "autonomy and Greenhouse provider policies must both allow search, fill, and submit"
    elif configuration_error:
        state = "CONFIGURATION_ERROR"
        reason = "answers and facts are required when policy enables auto-apply"
    else:
        plan = plan_batch(manifest, mode="plan", policy=policy)
        state = "PLANNED"
        reason = f"{len(plan.items)} approved jobs ready for the explicit apply/fill/submit policy"

    report = AutonomousRunReport(
        run_id=run_id,
        mode=mode,
        state=state,
        matrix_path=str(saved_matrix),
        shortlist_path=str(saved_shortlist),
        pipeline_path=str(saved_pipeline),
        state_path=str(state_path),
        queries_processed=len(runs),
        jobs_discovered=matched.raw_job_count,
        jobs_unique=len(matched.unique_jobs),
        approved=approved,
        target_submissions=target_total,
        approved_unapplied=approved_unapplied,
        submitted=submitted,
        target_reached=(len(submitted_ids) >= target_total) if execution_enabled else submitted >= target_total,
        counts=counts,
        pending_questions_count=(len(auto_report.to_dict().get("pending_questions", [])) if auto_report else 0),
        search_errors=tuple(errors),
        auto_apply=auto_report,
        reason=reason,
        campaign_path=str(effective_campaign_path) if execution_enabled else "",
        submitted_this_run=submitted,
        submitted_total=len(submitted_ids) if execution_enabled else submitted,
        remaining_target=max(target_total - len(submitted_ids), 0) if execution_enabled else max(target_total - submitted, 0),
        submit_attempts=submit_attempts,
    )
    _write_state(state_path, {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), **report.to_dict()})
    return report


__all__ = ["AutonomousRunReport", "run_autonomous"]
