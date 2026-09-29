"""Coleta explícita de respostas para blockers do auto-apply.

Este módulo só persiste valores digitados pelo operador. Não há fallback,
inferência semântica ou resposta automática para perguntas desconhecidas.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from .answers import AnswerLibrary
from .facts import FactStore


def _choose(value: str, options: list[str]) -> str:
    """Aceita índice humano ou opção exata; rejeita qualquer outra coisa."""

    candidate = value.strip()
    if candidate.isdigit():
        index = int(candidate) - 1
        if 0 <= index < len(options):
            return options[index]
    folded = candidate.casefold()
    for option in options:
        if option.casefold() == folded:
            return option
    raise ValueError("answer must be one of the listed options")


def collect_pending_questions(
    questions: Iterable[Mapping[str, object]],
    *,
    library: AnswerLibrary,
    facts: FactStore,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Pergunta cada blocker único e grava somente respostas explícitas.

    Fatos canônicos são gravados no ``FactStore``. Perguntas sem alias
    canônico permanecem na ``AnswerLibrary`` e são indexadas pelo prompt exato.
    """

    collected = 0
    for item in questions:
        fact_id = str(item.get("fact_id") or "")
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        used_by = item.get("used_by") or []
        if isinstance(used_by, list):
            labels = [
                f"{record.get('company', '')} — {record.get('title', '')}"
                for record in used_by
                if isinstance(record, Mapping)
            ]
            labels = [label.strip(" —") for label in labels if label.strip(" —")]
            if labels:
                output_fn(f"Usado por: {', '.join(labels)}")
        options = [str(option) for option in (item.get("options") or [])]
        output_fn(f"\n{fact_id}\n{question}")
        for index, option in enumerate(options, start=1):
            output_fn(f"  {index}. {option}")
        while True:
            answer = input_fn("Resposta: ").strip()
            try:
                value = _choose(answer, options) if options else answer
            except ValueError as exc:
                output_fn(str(exc))
                continue
            if not value:
                output_fn("answer must not be empty")
                continue
            if fact_id and not fact_id.startswith("question:"):
                facts.remember(fact_id, value, source="user")
            else:
                library.remember(question, value)
            collected += 1
            break
    return collected


__all__ = ["collect_pending_questions"]
