from __future__ import annotations

from .aliases import ALIASES
from .normalize import normalize_question

_NORMALIZED_ALIASES = {
    fact_id: {normalize_question(alias) for alias in aliases}
    for fact_id, aliases in ALIASES.items()
}


def canonical_fact_for(question: str) -> str | None:
    normalized = normalize_question(question)
    exact = [fact_id for fact_id, aliases in _NORMALIZED_ALIASES.items() if normalized in aliases]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    # Provedores costumam envolver a frase estavel em uma pergunta maior.
    # O casamento continua deterministico: frase inteira normalizada e uma
    # unica familia de fato; nunca fuzzy ou dependente da ordem.
    padded = f" {normalized} "
    contained = [
        fact_id
        for fact_id, aliases in _NORMALIZED_ALIASES.items()
        if any(f" {alias} " in padded for alias in aliases)
    ]
    return contained[0] if len(contained) == 1 else None


__all__ = ["canonical_fact_for"]
