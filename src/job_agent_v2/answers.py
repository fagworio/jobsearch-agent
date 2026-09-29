"""Resolucao de respostas por PRECEDENCIA EXPLICITA. Nada de inferencia.

1. resposta explicitamente aprovada (chave = prompt normalizado)
2. fato canônico aprovado
3. AnswerLibrary exata
4. campo trivial do profile
5. regra explícita
6. NEEDS_INPUT

Nao existe fuzzy matching, LLM, reuso semantico nem inferencia de vinculo
empregaticio: ausencia de evidencia NAO vira resposta.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from .facts import FactStore
from .models import Field, Form, MissingQuestion, Resolution
from .questions import canonical_fact_for
from .questions.normalize import normalize_question

#: Tipos em que um valor do profile pode ser usado sem julgamento.
TRIVIAL_KINDS = frozenset({"text", "email", "tel", "url", "textarea", "combobox", "number"})


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _index(items: Mapping[str, str] | Iterable[tuple[str, str]] | None) -> dict[str, str]:
    if items is None:
        return {}
    source = items.items() if isinstance(items, Mapping) else items
    return {_norm(key): str(value) for key, value in source}


def missing_fact_key(field: Field) -> str:
    """Stable blocker key; unknown questions remain grouped by normalized prompt."""
    return canonical_fact_for(field.prompt) or f"question:{field.identity}"


def _option_value(field: Field, value: str) -> str | None:
    """Converte um fato para uma opção real somente por regra determinística."""

    if not field.options:
        return value
    normalized = normalize_question(value)
    for option in field.options:
        if normalize_question(option) == normalized:
            return option

    # Valores canônicos curtos usados pelo perfil. A regra só aceita uma
    # opção única; se houver ambiguidade, o campo volta a NEEDS_INPUT.
    aliases = {
        "immediate": {"immediately", "as soon as possible", "0 15 days", "within 1 week"},
        "yes": {"yes", "true"},
        "no": {"no", "false"},
    }
    candidates = aliases.get(normalized, set())
    matches = [option for option in field.options if normalize_question(option) in candidates]
    if len(matches) == 1:
        return matches[0]

    # Greenhouse often expands safe boolean/time answers into descriptive
    # labels. Accept them only when the provider exposes one unambiguous
    # matching option; never choose among multiple sponsorship variants.
    if normalized in {"yes", "true", "no", "false"}:
        prefix = "yes" if normalized in {"yes", "true"} else "no"
        prefixed = [option for option in field.options if normalize_question(option).startswith(prefix + " ")]
        if len(prefixed) == 1:
            return prefixed[0]
    if normalized in {"immediate", "immediately", "as soon as possible"}:
        immediate = [option for option in field.options if "immediate" in normalize_question(option)]
        if len(immediate) == 1:
            return immediate[0]

    # Faixas de experiência: ``12+`` pode ser representado por ``10+ years``
    # ou por uma faixa que contenha 12. Só aceitamos uma opção inequívoca.
    number_match = re.match(r"^(\d+)", normalized)
    if number_match:
        years = int(number_match.group(1))
        numeric_matches: list[str] = []
        for option in field.options:
            option_norm = normalize_question(option)
            numbers = [int(value) for value in re.findall(r"\d+", option_norm)]
            if len(numbers) == 1 and ("more" in option_norm or "+" in option or "over" in option_norm):
                if years >= numbers[0]:
                    numeric_matches.append(option)
            elif len(numbers) >= 2 and ("-" in option or "to" in option_norm):
                if numbers[0] <= years <= numbers[1]:
                    numeric_matches.append(option)
        if len(numeric_matches) == 1:
            return numeric_matches[0]
    return None


def _deterministic_answer(
    field: Field,
    facts: FactStore | None,
    profile_index: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve only safe provider conventions with observable evidence.

    These rules deliberately require real options for provider choice widgets;
    a guessed label is never sent to the form.
    """

    prompt = _norm(field.prompt)
    if prompt in {"school", "school*"}:
        institution = (profile_index or {}).get("school") or (profile_index or {}).get("school*")
        if not institution:
            return None
        exact = _option_value(field, institution) if field.options else None
        if exact is not None:
            return exact
        other = [option for option in field.options if _norm(option) == "other"]
        # The profile confirms the actual institution. ``Other`` is used only
        # as the user's explicit fallback when that institution is absent from
        # the provider list (including an unopened lazy combobox).
        if not field.options or len(other) == 1 or not exact:
            return other[0] if other else "Other"

    if prompt in {"degree", "degree*"}:
        degree = (profile_index or {}).get("degree") or (profile_index or {}).get("degree*")
        if not degree:
            return None
        return _option_value(field, degree) if field.options and _option_value(field, degree) is not None else degree

    if "how did you hear about" in prompt or "how did you find out" in prompt:
        if not field.options:
            return None
        normalized = [(option, _norm(option)) for option in field.options]
        job_board = [option for option, value in normalized if "job board" in value]
        if len(job_board) == 1:
            return job_board[0]
        greenhouse = [option for option, value in normalized if "greenhouse" in value or "mygreenhouse" in value]
        if len(greenhouse) == 1:
            return greenhouse[0]
        return None

    if "additional details" in prompt and any(token in prompt for token in ("job board", "employee referral", "other")):
        if field.kind in {"text", "textarea"}:
            return "MyGreenhouse"
        return None

    if "region where you currently live" in prompt or "current region" in prompt:
        country = facts.get("identity.country") if facts is not None else None
        if country is None or _norm(country.value) not in {"brazil", "brasil"} or not field.options:
            return None
        normalized = [(option, _norm(option)) for option in field.options]
        regional = [
            option for option, value in normalized
            if "latin america" in value or value == "latam" or value == "brazil"
        ]
        return regional[0] if len(regional) == 1 else None

    if prompt in {"location", "location*"}:
        country = facts.get("identity.country") if facts is not None else None
        if country is None or _norm(country.value) not in {"brazil", "brasil"}:
            return None
        if field.options:
            matches = [option for option in field.options if "brazil" in _norm(option) or "brasil" in _norm(option)]
            return matches[0] if len(matches) == 1 else None
        return "Brazil"
    return None


def _provider_value(field: Field, value: str) -> str:
    """Adapt a confirmed profile value to the provider's visible label."""

    if field.kind == "combobox" and _norm(field.prompt) in {"country", "country*"} and not field.options:
        if _norm(value) in {"brazil", "brasil"}:
            return "Brazil+55"
    if field.kind == "tel" and _norm(field.prompt) in {"phone", "mobile", "telephone"}:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 13 and digits.startswith("55"):
            return f"+55 {digits[2:4]} {digits[4:9]}-{digits[9:]}"
    return value


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

    @classmethod
    def load_required(cls, path: str | Path) -> "AnswerLibrary":
        target = Path(path)
        if not target.exists():
            raise ValueError(f"answer library is required: {target}")
        return cls.load(target)

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
    facts: FactStore | None = None,
) -> Resolution:
    approved_index = _index(approved)
    profile_index = _index(profile)
    rules_index = _index(rules)

    answers: dict[str, str] = {}
    resolved_from: dict[str, str] = {}
    resolved_fact_ids: dict[str, str] = {}
    missing_fact_ids: dict[str, str] = {}
    missing: list[Field] = []
    missing_questions: list[MissingQuestion] = []

    def add_missing(field: Field, fact_id: str | None = None) -> None:
        missing.append(field)
        key = fact_id or missing_fact_key(field)
        missing_fact_ids[field.key] = key
        missing_questions.append(
            MissingQuestion(
                fact_id=key,
                field_id=field.key,
                question=field.prompt,
                options=field.options,
                required=field.required,
            )
        )

    for field in form.fields:
        identity = field.identity
        if identity in approved_index:
            value = _option_value(field, approved_index[identity])
            if value is None and field.required:
                add_missing(field)
                continue
            answers[field.key] = value if value is not None else approved_index[identity]
            resolved_from[field.key] = "approved_answer"
            continue
        fact_id = canonical_fact_for(field.prompt)
        fact = facts.get(fact_id) if facts is not None and fact_id else None
        if fact is not None:
            value = _option_value(field, fact.value)
            if value is not None:
                value = _provider_value(field, value)
            # A canonical fact may be a narrative evidence record while the
            # provider asks for a closed choice.  If the user has also
            # approved an exact provider answer for this prompt, use that
            # answer rather than attempting to type the narrative into the
            # combobox.  The explicit library entry remains the authority for
            # the provider's wording.
            if value is None and library is not None and field.options:
                saved = library.get(field.prompt)
                mapped_saved = _option_value(field, saved) if saved is not None else None
                if mapped_saved is not None:
                    answers[field.key] = mapped_saved
                    resolved_from[field.key] = "approved_answer_library"
                    continue
            if value is None and field.required:
                add_missing(field, fact.fact_id)
                continue
            answers[field.key] = value if value is not None else fact.value
            resolved_from[field.key] = "canonical_fact"
            resolved_fact_ids[field.key] = fact.fact_id
            continue
        saved = library.get(field.prompt) if library else None
        if saved is not None:
            value = _option_value(field, saved)
            if value is None and field.required:
                add_missing(field)
                continue
            answers[field.key] = value if value is not None else saved
            resolved_from[field.key] = "approved_answer_library"
            continue
        if field.kind in TRIVIAL_KINDS and identity in profile_index:
            if _norm(field.prompt) in {"school", "school*"}:
                value = _deterministic_answer(field, facts, profile_index)
            else:
                value = _option_value(field, profile_index[identity])
                if value is None:
                    value = _deterministic_answer(field, facts, profile_index)
            if value is not None:
                value = _provider_value(field, value)
            if value is None and field.required:
                add_missing(field)
                continue
            answers[field.key] = value if value is not None else profile_index[identity]
            resolved_from[field.key] = "profile"
            continue
        if identity in rules_index:
            value = _option_value(field, rules_index[identity])
            if value is None and field.required:
                add_missing(field)
                continue
            answers[field.key] = value if value is not None else rules_index[identity]
            resolved_from[field.key] = "rule"
            continue
        deterministic = _deterministic_answer(field, facts, profile_index)
        if deterministic is not None:
            answers[field.key] = deterministic
            resolved_from[field.key] = "deterministic_rule"
            continue
        if field.required:
            add_missing(field)
    return Resolution(
        answers=answers,
        resolved_from=resolved_from,
        resolved_fact_ids=resolved_fact_ids,
        missing_fact_ids=missing_fact_ids,
        missing=tuple(missing),
        missing_questions=tuple(missing_questions),
    )
