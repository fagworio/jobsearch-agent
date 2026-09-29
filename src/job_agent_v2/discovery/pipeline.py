"""Entrega da shortlist para os comandos atuais de apply/fill/submit."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PipelineItem:
    rank: int
    job_id: str
    url: str
    title: str
    company: str
    shortlist_score: float
    description: str = ""

    def command_payload(self, action: str) -> dict[str, Any]:
        if action not in {"apply", "fill", "submit"}:
            raise ValueError(f"unsupported pipeline action: {action}")
        return {
            "action": action,
            "job_id": self.job_id,
            "url": self.url,
            "title": self.title,
            "company": self.company,
            "shortlist_score": self.shortlist_score,
            "description": self.description,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "job_id": self.job_id,
            "url": self.url,
            "title": self.title,
            "company": self.company,
            "shortlist_score": self.shortlist_score,
            "commands": {action: self.command_payload(action) for action in ("apply", "fill", "submit")},
        }


@dataclass(frozen=True)
class PipelineManifest:
    source: str
    items: tuple[PipelineItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": "greenhouse",
            "source": self.source,
            "count": len(self.items),
            "items": [item.to_dict() for item in self.items],
        }


def _submission_was_processed(url: str, store: str | Path) -> bool:
    """Retorna true para qualquer tentativa que exige reconciliação explícita."""
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    marker = Path(store) / f"{key}.json"
    if not marker.exists():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Um marcador ilegível é fail-closed: não reencaminhar a vaga.
        return True
    return isinstance(payload, dict) and payload.get("outcome") in {
        "SUBMITTED",
        "SUBMIT_FAILED",
        "SUBMIT_UNKNOWN",
    }


def build_pipeline(
    shortlist_payload: dict[str, Any],
    *,
    submission_store: str | Path | None = None,
) -> PipelineManifest:
    if shortlist_payload.get("provider") != "greenhouse":
        raise ValueError("shortlist provider must be greenhouse")
    raw_entries = shortlist_payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("shortlist entries must be a list")
    items: list[PipelineItem] = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"shortlist entries[{index}] must be an object")
        if entry.get("selection") != "APPROVED" and entry.get("fit_decision") != "APPROVED":
            continue
        if entry.get("applied") is True:
            continue
        if not isinstance(entry.get("job_id"), str) or not isinstance(entry.get("url"), str) or not entry["url"].strip():
            raise ValueError(f"shortlist entries[{index}] has no valid job URL")
        if not isinstance(entry.get("rank"), int) or not isinstance(entry.get("shortlist_score"), (int, float)):
            raise ValueError(f"shortlist entries[{index}] has invalid ranking fields")
        if not all(isinstance(entry.get(key), str) for key in ("title", "company")):
            raise ValueError(f"shortlist entries[{index}] has invalid display fields")
        if submission_store is not None and _submission_was_processed(entry["url"], submission_store):
            continue
        items.append(PipelineItem(
            rank=entry["rank"],
            job_id=entry["job_id"],
            url=entry["url"],
            title=entry["title"],
            company=entry["company"],
            shortlist_score=float(entry["shortlist_score"]),
            description=str(entry.get("description") or ""),
        ))
    return PipelineManifest(str(shortlist_payload.get("source") or ""), tuple(items))


def load_shortlist(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("shortlist must contain a JSON object")
    return payload


def load_pipeline(path: str | Path) -> PipelineManifest:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("provider") != "greenhouse":
        raise ValueError("pipeline must contain a greenhouse manifest")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("pipeline items must be a list")
    items: list[PipelineItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"pipeline items[{index}] must be an object")
        try:
            items.append(PipelineItem(
                rank=int(raw["rank"]),
                job_id=str(raw["job_id"]),
                url=str(raw["url"]),
                title=str(raw["title"]),
                company=str(raw["company"]),
                shortlist_score=float(raw["shortlist_score"]),
                description=str(raw.get("description") or ""),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"pipeline items[{index}] is invalid") from exc
    return PipelineManifest(str(payload.get("source") or ""), tuple(items))


def save_pipeline(manifest: PipelineManifest, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination
