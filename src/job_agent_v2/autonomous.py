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
from .auto_apply import AutoApplyReport, run_auto_apply
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
    approved: int
    ready: int
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
            "approved": self.approved,
            "ready": self.ready,
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
    mode: str = "plan",
    profile_path: str = "profile/career_profile.local.yaml",
    matrix_path: str | Path = "data/v2-agent/matrix.json",
    shortlist_path: str | Path = "data/v2-agent/shortlist.json",
    pipeline_path: str | Path = "data/v2-agent/pipeline.json",
    state_path: str | Path = "data/v2-agent/state.json",
    policy_path: str = "profile/application_policy.yaml",
    target_ready_jobs: int = 3,
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
    if mode not in {"plan", "auto-apply"}:
        raise ValueError("mode must be plan or auto-apply")
    budget = SearchBudget(target_ready_jobs, max_queries, max_jobs_inspected, max_pages)
    budget.validate()
    run_id = uuid4().hex
    adapter = GreenhouseDiscoveryAdapter()
    runs: list[DiscoverySearchRun] = []
    errors: list[str] = []
    cursor = SearchCursor()

    with NativeMessagingClient() as browser:
        initial = adapter.inspect(browser.inspect_discovery_results())
        work_type = initial.work_type or ("remote",)
        try:
            for item in build_query_matrix():
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
                except Exception as exc:  # noqa: BLE001 - uma consulta não bloqueia o lote
                    errors.append(f"{item.query}: {exc}")
                    cursor = SearchCursor(
                        queries_processed=cursor.queries_processed + 1,
                        jobs_inspected=cursor.jobs_inspected,
                        pages=cursor.pages + 1,
                        ready_jobs=cursor.ready_jobs,
                    )
        finally:
            browser.discover_query(initial.query or "frontend", list(work_type))

    matrix = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
    matched = match_matrix(matrix, load_match_profile(profile_path))
    saved_matrix = save_matrix(matched, matrix_path)
    shortlist = rank_shortlist(matched, source=str(saved_matrix))
    saved_shortlist = save_shortlist(shortlist, shortlist_path)
    manifest = build_pipeline(shortlist.to_dict(), submission_store=marker_store)
    saved_pipeline = save_pipeline(manifest, pipeline_path)
    approved = sum(1 for item in manifest.items)
    ready = sum(1 for item in shortlist.entries if item.selection == "APPROVED" and not item.applied)

    policy = load_policy(policy_path)
    if mode == "auto-apply":
        greenhouse = ((policy.get("providers") or {}).get("greenhouse") or {})
        if greenhouse.get("submit") != "auto":
            state = "POLICY_BLOCKED"
            reason = "providers.greenhouse.submit is not auto"
            auto_report = None
        elif answers is None or facts is None:
            state = "CONFIGURATION_ERROR"
            reason = "answers and facts are required for auto-apply"
            auto_report = None
        else:
            auto_report = run_auto_apply(
                manifest,
                resume=resume,
                resume_profile=resume_profile,
                resume_store=resume_store,
                library=answers,
                facts=facts,
                marker_store=marker_store,
                report_store=report_store,
                max_jobs=max_jobs,
                max_submits=max_submits,
                max_failures=max_failures,
                human_wait_ms=human_wait_ms,
            )
            state = "COMPLETED"
            reason = ""
    else:
        auto_report = None
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
        ready=ready,
        search_errors=tuple(errors),
        auto_apply=auto_report,
        reason=reason,
    )
    _write_state(state_path, {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), **report.to_dict()})
    return report


__all__ = ["AutonomousRunReport", "run_autonomous"]
