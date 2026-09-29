"""Migração conservadora de respostas textuais aprovadas para fatos V2."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

import yaml

from .facts import FactStore
from .questions import canonical_fact_for


def load_approved_answers(path: str | Path) -> dict[str, str]:
    """Load only explicitly approved legacy answers.

    V1 stores either a flat JSON answer map or a YAML ``answers`` list with
    provenance and approval flags. Unknown shapes fail closed.
    """
    target = Path(path)
    if not target.exists():
        raise ValueError(f"answer source is required: {target}")
    text = target.read_text(encoding="utf-8")
    payload = json.loads(text) if target.suffix.lower() == ".json" else yaml.safe_load(text)
    if isinstance(payload, Mapping):
        if "answers" in payload:
            entries = payload.get("answers")
            if not isinstance(entries, list):
                raise ValueError("legacy answers must be a list")
            result: dict[str, str] = {}
            for item in entries:
                if not isinstance(item, Mapping) or item.get("approved") is not True:
                    continue
                prompt = item.get("question")
                answer = item.get("answer")
                if isinstance(prompt, str) and prompt.strip() and isinstance(answer, str) and answer.strip():
                    result[prompt] = answer
            return result
        if all(isinstance(key, str) and isinstance(value, str) for key, value in payload.items()):
            return {str(key): str(value) for key, value in payload.items() if str(value).strip()}
    raise ValueError("answer source must be a flat map or an approved answers list")


@dataclass(frozen=True)
class MigrationReport:
    migrated: int = 0
    already_present: int = 0
    ambiguous: int = 0
    rejected: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "migrated": self.migrated,
            "already_present": self.already_present,
            "ambiguous": self.ambiguous,
            "rejected": self.rejected,
        }


def migrate_answers(answers: Mapping[str, str], facts: FactStore) -> MigrationReport:
    migrated = already_present = ambiguous = rejected = 0
    for prompt, value in answers.items():
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(value, str) or not value.strip():
            rejected += 1
            continue
        fact_id = canonical_fact_for(prompt)
        if fact_id is None:
            ambiguous += 1
            continue
        if facts.get(fact_id) is not None:
            already_present += 1
            continue
        facts.remember(fact_id, value, source="migrated_v1")
        migrated += 1
    return MigrationReport(migrated, already_present, ambiguous, rejected)


__all__ = ["MigrationReport", "load_approved_answers", "migrate_answers"]
