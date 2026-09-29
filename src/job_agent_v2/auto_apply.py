"""Fluxo explícito de auto-apply para uma janela operacional limitada.

Este é o único caminho que encadeia descoberta aprovada, abertura da vaga e
submit. A entrada é o pipeline já aprovado; não há matching ou respostas
inventadas nesta camada. O lote é sequencial e controlado por budgets explícitos.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .answers import AnswerLibrary
from .browser import NativeMessagingClient
from .discovery.pipeline import PipelineManifest, PipelineItem
from .facts import FactStore
from .submit import SubmitReport, submit


@dataclass(frozen=True)
class AutoApplyItem:
    job_id: str
    title: str
    company: str
    url: str
    state: str
    reason: str = ""
    tab_id: int | None = None
    submit: dict[str, Any] | None = None
    missing_facts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "state": self.state,
            "reason": self.reason,
            "missing_facts": list(self.missing_facts),
            "tab_id": self.tab_id,
            "submit": self.submit or {},
        }


@dataclass(frozen=True)
class AutoApplyReport:
    source: str
    mode: str
    max_jobs: int
    max_submits: int
    max_failures: int
    parallelism: int
    items: tuple[AutoApplyItem, ...]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        pending_facts: set[str] = set()
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
            if item.state == "NEEDS_INPUT":
                pending_facts.update(item.missing_facts)
        return {
            "provider": "greenhouse",
            "source": self.source,
            "mode": self.mode,
            "limits": {
                "max_jobs": self.max_jobs,
                "max_submits": self.max_submits,
                "max_failures": self.max_failures,
                "parallelism": self.parallelism,
            },
            "counts": counts,
            "pending_facts": sorted(pending_facts),
            "items": [item.to_dict() for item in self.items],
        }


def _write_report(report: AutoApplyReport, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination


def _approved_items(manifest: PipelineManifest, max_jobs: int) -> tuple[PipelineItem, ...]:
    if max_jobs <= 0:
        raise ValueError("max_jobs must be positive")
    return tuple(sorted(manifest.items, key=lambda item: item.rank)[:max_jobs])


def _validate_limits(*, max_jobs: int, max_submits: int, max_failures: int, parallelism: int) -> None:
    if max_jobs <= 0:
        raise ValueError("max_jobs must be positive")
    if max_submits < 0:
        raise ValueError("max_submits must be non-negative")
    if max_failures < 0:
        raise ValueError("max_failures must be non-negative")
    if parallelism != 1:
        raise ValueError("parallelism must remain 1 for the first batch rollout")


def _is_hard_failure(state: str) -> bool:
    return state in {"FAILED", "FORM_NOT_FOUND", "SUBMIT_FAILED"}


def run_auto_apply(
    manifest: PipelineManifest,
    *,
    resume: str,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    library: AnswerLibrary | None = None,
    facts: FactStore | None = None,
    marker_store: str = "data/v2-submissions",
    report_store: str | Path = "data/v2-auto/auto-apply.json",
    max_jobs: int = 5,
    max_submits: int = 3,
    max_failures: int = 2,
    parallelism: int = 1,
    human_wait_ms: int = 10 * 60 * 1000,
    settle_ms: int = 20_000,
) -> AutoApplyReport:
    """Executa a janela auto-apply já iniciada pelo comando explícito."""

    _validate_limits(
        max_jobs=max_jobs,
        max_submits=max_submits,
        max_failures=max_failures,
        parallelism=parallelism,
    )
    if not resume:
        raise ValueError("resume is required for auto-apply")

    selected = _approved_items(manifest, max_jobs)
    results: list[AutoApplyItem] = []
    submits = 0
    failures = 0
    halted = False
    for item in selected:
        if halted:
            results.append(AutoApplyItem(item.job_id, item.title, item.company, item.url, "BATCH_HALTED", "previous item produced SUBMIT_UNKNOWN"))
            continue
        if submits >= max_submits:
            results.append(AutoApplyItem(item.job_id, item.title, item.company, item.url, "LIMIT_REACHED", "max_submits reached"))
            continue
        tab_id: int | None = None
        try:
            with NativeMessagingClient() as browser:
                context = browser.open_job(item.job_id, item.url, "greenhouse")
                tab_id_value = context.get("tab_id")
                if not isinstance(tab_id_value, int):
                    raise RuntimeError("OPEN_JOB did not return a valid tab_id")
                tab_id = tab_id_value
                observed = browser.get_tab_context(tab_id)
                if observed.get("job_id") != item.job_id or observed.get("provider") != "greenhouse":
                    raise RuntimeError("tab context does not match pipeline item")
                browser.wait_for_application(tab_id, item.job_id, "greenhouse")

            report: SubmitReport = submit(
                item.url,
                approved=approved,
                profile=profile,
                rules=rules,
                library=library,
                facts=facts,
                resume=resume,
                store=marker_store,
                settle_ms=settle_ms,
                human_wait_ms=human_wait_ms,
                tab_id=tab_id,
                provider="greenhouse",
                canonical_job_id=item.job_id,
            )
            if report.submission_writes:
                submits += 1
            state = report.state.value
            if report.reason == "missing_answer":
                state = "NEEDS_INPUT"
            elif report.state.value == "HUMAN_REQUIRED":
                state = "WAITING_HUMAN"
            results.append(AutoApplyItem(
                item.job_id,
                item.title,
                item.company,
                item.url,
                state,
                report.reason,
                tab_id,
                report.to_dict(),
                report.missing_facts,
            ))
            if state == "SUBMIT_UNKNOWN":
                halted = True
            elif _is_hard_failure(state):
                failures += 1
                if failures >= max_failures:
                    halted = True
        except RuntimeError as exc:
            reason = str(exc)
            state = "FORM_NOT_FOUND" if "FORM_NOT_FOUND" in reason else "FAILED"
            results.append(AutoApplyItem(item.job_id, item.title, item.company, item.url, state, reason, tab_id))
            failures += 1
            if failures >= max_failures:
                halted = True
        except Exception as exc:  # noqa: BLE001 - cada item fica isolado
            results.append(AutoApplyItem(item.job_id, item.title, item.company, item.url, "FAILED", str(exc), tab_id))
            failures += 1
            if failures >= max_failures:
                halted = True

    report = AutoApplyReport(manifest.source, "auto-apply", max_jobs, max_submits, max_failures, parallelism, tuple(results))
    _write_report(report, report_store)
    return report


__all__ = ["AutoApplyItem", "AutoApplyReport", "run_auto_apply"]
