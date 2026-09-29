from __future__ import annotations

from .aliases import ALIASES
from .normalize import normalize_question

_NORMALIZED_ALIASES = {
    fact_id: {normalize_question(alias) for alias in aliases}
    for fact_id, aliases in ALIASES.items()
}


def canonical_fact_for(question: str) -> str | None:
    normalized = normalize_question(question)
    for fact_id, aliases in _NORMALIZED_ALIASES.items():
        if normalized in aliases:
            return fact_id
    return None


__all__ = ["canonical_fact_for"]
