"""Loop único de discovery, shortlist, pipeline e execução controlada.

O módulo junta as etapas que antes precisavam ser chamadas manualmente, mas
mantém os limites e a política como fronteiras explícitas. O modo padrão é
somente planejamento; execução real só é liberada quando a política local
declara ``submit: auto``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from .facts import FactStore


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
    search_errors: tuple[str, ...] = ()
    auto_apply: AutoApplyReport | None = None
    reason: str = ""

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
            "target_reached": self.target_reached,
            "counts": dict(self.counts),
            "search_errors": list(self.search_errors),
        }
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
    max_queries: int = 20,
    max_jobs_inspected: int = 250,
    max_pages: int = 40,
    answers: AnswerLibrary | None = None,
    facts: FactStore | None = None,
    resume: str = "",
    resume_profile: str = "profile/career_profile.local.yaml",
    resume_store: str = "data/v2-resumes",
    marker_store: str = "data/v2-submissions",
    report_store: str | Path = "data/v2-auto/auto-apply.json",
    max_jobs: int = 5,
    max_submits: int = 3,
    max_failures: int = 2,
    human_wait_ms: int = 0,
) -> AutonomousRunReport:
    if mode not in {"policy", "plan", "auto-apply"}:
        raise ValueError("mode must be policy, plan or auto-apply")
    if target_ready_jobs is not None:
        target_submissions = target_ready_jobs
    if target_submissions < 0:
        raise ValueError("target_submissions must be non-negative")
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
    applied_items: list[AutoApplyItem] = []
    jobs_processed = 0
    submit_attempts = 0
    submitted = 0
    stop_unknown = False

    with NativeMessagingClient() as browser:
        initial = adapter.inspect(browser.inspect_discovery_results())
        work_type = initial.work_type or ("remote",)
        try:
            for item in build_query_matrix():
                if stop_unknown or jobs_processed >= max_jobs or submit_attempts >= max_submits:
                    break
                if not cursor.should_continue(budget):
                    break
                try:
                    snapshot = browser.discover_query(item.query, list(work_type))
                    result = adapter.inspect(snapshot)
                    runs.append(DiscoverySearchRun(item.family, item.query, result))
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
                        candidate_manifest = build_pipeline(shortlist.to_dict(), submission_store=marker_store)
                        candidates = tuple(item for item in candidate_manifest.items if item.job_id not in processed_ids)
                        batch_size = min(max_jobs - jobs_processed, len(candidates))
                        attempts_left = max_submits - submit_attempts
                        if batch_size > 0 and attempts_left > 0:
                            batch_manifest = type(candidate_manifest)(candidate_manifest.source, candidates[:batch_size])
                            batch_report = run_auto_apply(
                                batch_manifest,
                                resume=resume,
                                resume_profile=resume_profile,
                                resume_store=resume_store,
                                library=answers,
                                facts=facts,
                                marker_store=marker_store,
                                report_store=report_store,
                                max_jobs=batch_size,
                                max_submits=attempts_left,
                                max_failures=max_failures,
                                human_wait_ms=human_wait_ms,
                            )
                            applied_items.extend(batch_report.items)
                            processed_ids.update(item.job_id for item in batch_report.items)
                            jobs_processed += len(batch_report.items)
                            submit_attempts += sum(
                                int((item.submit or {}).get("submission_writes", 0))
                                for item in batch_report.items
                            )
                            submitted += sum(1 for item in batch_report.items if item.state == "SUBMITTED")
                            stop_unknown = any(item.state == "SUBMIT_UNKNOWN" for item in batch_report.items)
                            if submitted >= target_submissions:
                                break
                except Exception as exc:  # noqa: BLE001 - uma consulta não bloqueia o lote
                    errors.append(f"{item.query}: {exc}")
                    cursor = SearchCursor(
                        queries_processed=cursor.queries_processed + 1,
                        jobs_inspected=cursor.jobs_inspected,
                        pages=cursor.pages + 1,
                        ready_jobs=cursor.ready_jobs,
                    )
        finally:
            try:
                browser.discover_query(initial.query or "frontend", list(work_type))
            except (OSError, RuntimeError) as exc:
                # Restoring the operator's original search is best effort. A
                # Chrome/native-host disconnect here must not erase the
                # ledger or hide results already persisted by auto-apply.
                errors.append(f"restore initial query: {exc}")

    matrix = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
    matched = match_matrix(matrix, load_match_profile(profile_path))
    saved_matrix = save_matrix(matched, matrix_path)
    shortlist = rank_shortlist(matched, source=str(saved_matrix))
    saved_shortlist = save_shortlist(shortlist, shortlist_path)
    manifest = build_pipeline(shortlist.to_dict(), submission_store=marker_store)
    saved_pipeline = save_pipeline(manifest, pipeline_path)
    approved = len(manifest.items)
    approved_unapplied = sum(1 for item in shortlist.entries if item.selection == "APPROVED" and not item.applied)
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
        )
        _write_report(auto_report, report_store)
        state = "COMPLETED" if submitted >= target_submissions else ("SUBMIT_UNKNOWN" if stop_unknown else "TARGET_NOT_REACHED")
        reason = "" if state == "COMPLETED" else "search or application budgets exhausted before target_submissions"
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
        target_submissions=target_submissions,
        approved_unapplied=approved_unapplied,
        submitted=submitted,
        target_reached=submitted >= target_submissions,
        counts=counts,
        search_errors=tuple(errors),
        auto_apply=auto_report,
        reason=reason,
    )
    _write_state(state_path, {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), **report.to_dict()})
    return report


__all__ = ["AutonomousRunReport", "run_autonomous"]
