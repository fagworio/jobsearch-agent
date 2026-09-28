"""Resolucao de respostas por PRECEDENCIA EXPLICITA. Nada de inferencia.

1. resposta explicitamente aprovada (chave = prompt normalizado)
2. campo trivial do profile (so para tipos texto/email/tel)
3. regra explicita
4. NEEDS_INPUT

Nao existe fuzzy matching, LLM, reuso semantico nem inferencia de vinculo
empregaticio: ausencia de evidencia NAO vira resposta.
"""

from __future__ import annotations

import json
from pathlib import Path
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


class AnswerLibrary:
    """Biblioteca local de respostas aprovadas explicitamente pelo usuário.

    A biblioteca só muda através de ``remember``; resolver uma resposta não
    grava nada automaticamente. Isso impede que uma inferência ou uma resposta
    errada se torne fato reutilizável sem uma decisão explícita.
    """

    def __init__(self, answers: Mapping[str, str] | None = None) -> None:
        self._answers = {_norm(key): str(value) for key, value in (answers or {}).items() if str(value).strip()}

    @classmethod
    def load(cls, path: str | Path) -> "AnswerLibrary":
        target = Path(path)
        if not target.exists() or not target.read_text(encoding="utf-8").strip():
            return cls()
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("answer library must contain a JSON object")
        return cls({str(key): str(value) for key, value in payload.items()})

    def remember(self, prompt: str, answer: str) -> None:
        key = _norm(prompt)
        if not key:
            raise ValueError("prompt must not be empty")
        if not str(answer).strip():
            raise ValueError("answer must not be empty")
        self._answers[key] = str(answer)

    def get(self, prompt: str) -> str | None:
        return self._answers.get(_norm(prompt))

    def as_dict(self) -> dict[str, str]:
        return dict(self._answers)

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(self._answers, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)


def resolve(
    form: Form,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    library: AnswerLibrary | None = None,
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
        saved = library.get(field.prompt) if library else None
        if saved is not None:
            answers[field.key] = saved
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
