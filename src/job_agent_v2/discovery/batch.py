"""Orquestração autônoma segura para um pipeline aprovado."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .pipeline import PipelineManifest


@dataclass(frozen=True)
class BatchItem:
    job_id: str
    action: str
    state: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"job_id": self.job_id, "action": self.action, "state": self.state, "reason": self.reason}


@dataclass(frozen=True)
class BatchReport:
    source: str
    mode: str
    items: tuple[BatchItem, ...]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
        return {
            "source": self.source,
            "mode": self.mode,
            "counts": counts,
            "items": [item.to_dict() for item in self.items],
        }


def load_policy(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("application policy must contain a mapping")
    return payload


def plan_batch(manifest: PipelineManifest, *, mode: str = "plan", policy: dict[str, Any] | None = None) -> BatchReport:
    if mode not in {"plan", "apply", "fill", "submit"}:
        raise ValueError(f"unsupported batch mode: {mode}")
    policy = policy or {}
    greenhouse = ((policy.get("providers") or {}).get("greenhouse") or {})
    fill_policy = greenhouse.get("fill_forms", "review")
    submit_policy = greenhouse.get("submit", "manual")
    items: list[BatchItem] = []
    for item in manifest.items:
        if mode == "plan":
            items.append(BatchItem(item.job_id, "apply", "READY", "queued for the current apply/fill/submit flow"))
        elif mode == "apply":
            items.append(BatchItem(item.job_id, "apply", "READY", "apply is read-only; open the item in the current browser session"))
        elif mode == "fill":
            state = "MANUAL_REVIEW_REQUIRED" if fill_policy != "auto" else "READY"
            reason = f"provider fill_forms policy is {fill_policy}"
            items.append(BatchItem(item.job_id, "fill", state, reason))
        else:
            state = "MANUAL_CONFIRMATION_REQUIRED" if submit_policy != "auto" else "READY"
            reason = f"provider submit policy is {submit_policy}"
            items.append(BatchItem(item.job_id, "submit", state, reason))
    return BatchReport(manifest.source, mode, tuple(items))


def save_batch(report: BatchReport, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination
