"""Armazenamento local de fatos canônicos explicitamente aprovados."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CanonicalFact:
    fact_id: str
    value: str
    approved: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "approved": self.approved, "source": self.source}


class FactStore:
    """Facts are reusable only when the stored record is explicitly approved."""

    def __init__(self, facts: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._facts: dict[str, CanonicalFact] = {}
        for fact_id, payload in (facts or {}).items():
            if not isinstance(payload, Mapping):
                continue
            value = payload.get("value")
            approved = payload.get("approved")
            source = payload.get("source", "")
            if isinstance(value, str) and value.strip() and approved is True and isinstance(source, str) and source.strip():
                self._facts[str(fact_id)] = CanonicalFact(str(fact_id), value, True, source)

    @classmethod
    def load(cls, path: str | Path) -> "FactStore":
        target = Path(path)
        if not target.exists() or not target.read_text(encoding="utf-8").strip():
            return cls()
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("fact store must contain a JSON object")
        return cls(payload)

    @classmethod
    def load_required(cls, path: str | Path) -> "FactStore":
        target = Path(path)
        if not target.exists():
            raise ValueError(f"fact store is required: {target}")
        return cls.load(target)

    def get(self, fact_id: str) -> CanonicalFact | None:
        return self._facts.get(fact_id)

    def remember(self, fact_id: str, value: str, *, source: str = "user") -> None:
        if not fact_id.strip() or not value.strip() or not source.strip():
            raise ValueError("fact_id, value and source are required")
        self._facts[fact_id] = CanonicalFact(fact_id, value, True, source)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {fact_id: fact.to_dict() for fact_id, fact in self._facts.items()}

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)


__all__ = ["CanonicalFact", "FactStore"]
