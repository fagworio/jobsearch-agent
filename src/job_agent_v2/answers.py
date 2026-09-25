"""Resolucao de respostas por PRECEDENCIA EXPLICITA. Nada de inferencia.

1. resposta explicitamente aprovada (chave = prompt normalizado)
2. campo trivial do profile (so para tipos texto/email/tel)
3. regra explicita
4. NEEDS_INPUT

Nao existe fuzzy matching, LLM, reuso semantico nem inferencia de vinculo
empregaticio: ausencia de evidencia NAO vira resposta.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .models import Field, Form, Resolution

#: Tipos em que um valor do profile pode ser usado sem julgamento.
TRIVIAL_KINDS = frozenset({"text", "email", "tel", "url", "textarea"})


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _index(items: Mapping[str, str] | Iterable[tuple[str, str]] | None) -> dict[str, str]:
    if items is None:
        return {}
    source = items.items() if isinstance(items, Mapping) else items
    return {_norm(key): str(value) for key, value in source}


def resolve(
    form: Form,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
) -> Resolution:
    approved_index = _index(approved)
    profile_index = _index(profile)
    rules_index = _index(rules)

    answers: dict[str, str] = {}
    missing: list[Field] = []
    for field in form.fields:
        identity = field.identity
        if identity in approved_index:
            answers[field.key] = approved_index[identity]
            continue
        if field.kind in TRIVIAL_KINDS and identity in profile_index:
            answers[field.key] = profile_index[identity]
            continue
        if identity in rules_index:
            answers[field.key] = rules_index[identity]
            continue
        if field.required:
            missing.append(field)
    return Resolution(answers=answers, missing=tuple(missing))
