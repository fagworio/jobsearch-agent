"""Deterministic, provenance-aware question and answer resolution."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from .models import ApplicationAnswer, ApplicationField, CandidatePreferences, CareerProfile


AUTO_FILL_CONFIDENCE = 0.85

_IDENTITY_SEMANTICS = {
    "first_name": "first_name",
    "last_name": "last_name",
    "preferred_first_name": "preferred_first_name",
    "full_name": "name",
    "email": "email",
    "phone": "phone",
    "country": "country",
    "current_location": "current_location",
    "linkedin": "linkedin",
    "github": "github",
    "website": "website",
}


_COUNTRY_ALIASES = {
    "brazil": "Brazil",
    "brasil": "Brazil",
    "united states": "United States",
    "usa": "United States",
    "u s": "United States",
    "canada": "Canada",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "portugal": "Portugal",
}


LEGAL_TERMS = ("legal", "law", "declaration", "declare", "conviction", "criminal", "accommod", "disability", "ethnicity", "race", "visto", "autorização de trabalho", "autorizacao de trabalho")


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks.lower()).strip()


def question_key(question: str) -> str:
    normalized = _normalize(question)
    return "q-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _field_country(field: ApplicationField) -> str:
    explicit = field.semantic_context.get("country", "") if field.semantic_context else ""
    if explicit:
        return str(explicit)
    text = _normalize(f"{field.key} {field.label}")
    for alias, country in sorted(_COUNTRY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(_normalize(alias))}(?![a-z])", text):
            return country
    return ""


def is_legal_question(question: str) -> bool:
    normalized = _normalize(question)
    return any(term in normalized for term in LEGAL_TERMS)


def _current_experience(profile: CareerProfile):
    """Experiencia atual: entre as abertas, a de maior inicio.

    Assumir "primeiro item da lista" so funcionaria se o YAML estivesse
    ordenado por acaso. A comparacao e lexicografica sobre a data textual, o
    que basta para o formato ISO usado no perfil.
    """
    open_roles = [item for item in profile.experiences if not item.end_date]
    pool = open_roles or list(profile.experiences)
    if not pool:
        return None
    return max(pool, key=lambda item: (item.start_date or "", item.end_date or ""))


def load_answers(path: str | Path) -> list[ApplicationAnswer]:
    source = Path(path)
    if not source.exists():
        return []
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    entries = raw.get("answers", []) if isinstance(raw, dict) else []
    if isinstance(entries, dict):
        entries = [{"question_key": key, **(value if isinstance(value, dict) else {"answer": value})} for key, value in entries.items()]
    result: list[ApplicationAnswer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question", entry.get("question_key", "")))
        answer = str(entry.get("answer", ""))
        if not question or not answer:
            continue
        result.append(ApplicationAnswer(
            question_key=str(entry.get("question_key") or question_key(question)),
            question=question,
            answer=answer,
            supported_by=[str(item) for item in entry.get("supported_by", [])],
            source=str(entry.get("source", "approved_answer")),
            confidence=float(entry.get("confidence", 1.0)),
            approved=bool(entry.get("approved", True)),
            legal=bool(entry.get("legal", is_legal_question(question))),
            semantic_type=str(entry.get("semantic_type", "unknown")),
            field_key=str(entry.get("field_key", "")),
        ))
    return result



#: Marcadores de opcao que recusam a autodeclaracao, por ATS.
#: Comparados sobre o rotulo normalizado (pontuacao vira espaco), portanto
#: escritos sem apostrofo. Sao frases negativas completas de proposito: um
#: marcador parcial como "wish to answer" casaria tambem com "I wish to
#: answer", que e o oposto da recusa.
DECLINE_MARKERS = (
    "decline to self identify",
    "decline",
    "do not wish to answer",
    "don t wish to answer",
    "do not want to answer",
    "don t want to answer",
    "prefer not to answer",
    "prefer not",
    "not to answer",
)

DECLINE_SOURCE = "decline_self_identification"

#: Rotulos de aceite variam por ATS ("Yes", "Acknowledge/Confirm", "I accept").
#: "yes" fica por ultimo e so e usado quando nenhum marcador especifico casa,
#: para nao confundir uma pergunta de deficiencia ("Yes, I have a disability")
#: com um consentimento.
AFFIRM_SOURCE = "affirm_consent"
AFFIRM_MARKERS = ("i agree", "agree", "acknowledge", "confirm", "i accept", "i consent", "yes")


@dataclass(frozen=True)
class AnswerRule:
    """Resposta reutilizavel casada pelo significado da pergunta.

    Campos se repetem entre vagas com redacoes diferentes ("Email", "E-mail
    address", "Your email"). Uma regra casa por trecho normalizado e resolve a
    partir do perfil, das preferencias ou de um valor literal, para nao exigir
    uma resposta nova a cada formulario.
    """

    name: str
    match: tuple[str, ...]
    answer: str = ""
    source: str = ""
    decline: bool = False

    def matches(self, label: str) -> bool:
        normalized = _normalize(label)
        return any(_normalize(term) in normalized for term in self.match)


def load_rules(path: str | Path) -> list[AnswerRule]:
    source = Path(path)
    if not source.exists():
        return []
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    entries = raw.get("rules", []) if isinstance(raw, dict) else []
    rules: list[AnswerRule] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        match = entry.get("match") or []
        if isinstance(match, str):
            match = [match]
        match = tuple(str(item) for item in match if str(item).strip())
        if not match:
            continue
        rules.append(AnswerRule(
            name=str(entry.get("name", "")),
            match=match,
            answer=str(entry.get("answer", "")),
            source=str(entry.get("from", "")),
            decline=bool(entry.get("decline", False)),
        ))
    return rules


class AnswerKnowledgeBase:
    def __init__(self, answers: list[ApplicationAnswer], rules: list[AnswerRule] | None = None):
        self.answers = [answer for answer in answers if answer.approved and answer.answer]
        self.rules = list(rules or [])

    def resolve(self, question: str, profile: CareerProfile, preferences: CandidatePreferences | None = None) -> ApplicationAnswer | None:
        normalized = _normalize(question)
        legal = is_legal_question(question)
        exact = next((answer for answer in self.answers if _normalize(answer.question) == normalized or answer.question_key == question_key(question)), None)
        if exact:
            return exact
        structured = self._resolve_profile(question, profile, preferences)
        if structured:
            return structured
        if legal:
            return None
        scored = sorted(((difflib.SequenceMatcher(None, normalized, _normalize(answer.question)).ratio(), answer) for answer in self.answers), key=lambda item: item[0], reverse=True)
        if scored and scored[0][0] >= 0.88:
            answer = scored[0][1]
            return ApplicationAnswer(answer.question_key, question, answer.answer, list(answer.supported_by), "approved_semantic", scored[0][0], True, answer.legal, answer.semantic_type)
        return self._resolve_profile(question, profile, preferences)

    def _resolve_rule(self, field: ApplicationField, profile: CareerProfile, preferences: CandidatePreferences | None) -> ApplicationAnswer | None:
        """Aplica a politica reutilizavel casada pelo significado da pergunta."""
        for rule in self.rules:
            if not rule.matches(field.label):
                continue
            if rule.decline:
                return self._field_answer(
                    field,
                    "Decline to self-identify",
                    [f"answer_policy:{rule.name}"],
                    DECLINE_SOURCE,
                    1.0,
                    field.semantic_type,
                )
            value = ""
            if rule.source == "identity.email":
                value = str(profile.identity.get("email", ""))
            elif rule.source == "identity.country":
                value = str(profile.identity.get("country", ""))
            elif rule.source == "identity.phone":
                value = str(profile.identity.get("phone", ""))
            elif rule.source == "work_authorization":
                country = _field_country(field)
                authorized = {_normalize(item) for item in (preferences.work_authorization if preferences else [])}
                value = "Yes" if country and _normalize(country) in authorized else "No"
            elif rule.source == "requires_sponsorship":
                wants = bool(preferences) and preferences.requires_sponsorship == "yes"
                value = "Yes" if wants else "No"
            elif rule.answer:
                value = rule.answer
            if not value:
                continue
            if field.options and not self._option_matches(value, field.options):
                continue
            source = "AnswerPolicy"
            if rule.answer and any(marker in _normalize(rule.answer) for marker in ("agree", "consent", "accept")):
                source = AFFIRM_SOURCE
            return self._field_answer(field, value, [f"answer_policy:{rule.name}"], source, 1.0, field.semantic_type)
        return None

    def _exact_answer(self, question: str) -> ApplicationAnswer | None:
        normalized = _normalize(question)
        return next(
            (
                answer
                for answer in self.answers
                if _normalize(answer.question) == normalized or answer.question_key == question_key(question)
            ),
            None,
        )

    def resolve_field(self, field: ApplicationField, profile: CareerProfile, preferences: CandidatePreferences | None = None) -> ApplicationAnswer | None:
        """Resolve a field using semantic type and options before text matching."""
        # Uma resposta aprovada que casa exatamente com a pergunta nao e inferencia
        # semantica: e o texto que o candidato aprovou. Vale mesmo quando o
        # adapter nao reconheceu o campo (confidence 0.0), que e o caso de toda
        # pergunta customizada de um board real.
        exact = self._exact_answer(field.label)
        if exact:
            if field.options and not self._option_matches(exact.answer, field.options):
                return None
            # Uma resposta de aceite ("I agree") vale em qualquer ATS, cujo
            # rotulo varia ("Yes", "Acknowledge/Confirm", "I accept").
            if any(marker in _normalize(exact.answer) for marker in ("agree", "consent", "accept")):
                exact = replace(exact, source=AFFIRM_SOURCE)
            return replace(exact, semantic_type=field.semantic_type, field_key=field.key)
        # Politica reutilizavel: cobre perguntas que se repetem entre vagas com
        # redacoes diferentes, inclusive as que o adapter nao reconhece.
        ruled = self._resolve_rule(field, profile, preferences)
        if ruled:
            return ruled
        if field.confidence < AUTO_FILL_CONFIDENCE:
            return None
        semantic_type = field.semantic_type
        normalized_label = _normalize(field.label)
        if semantic_type == "unknown":
            if normalized_label in {"full name", "nome completo"}:
                semantic_type = "full_name"
            elif "email" in normalized_label or "e mail" in normalized_label:
                semantic_type = "email"
            elif "authorized to work" in normalized_label or "autorizacao de trabalho" in normalized_label:
                semantic_type = "work_authorization"
            elif "sponsorship" in normalized_label or "patrocinio" in normalized_label:
                semantic_type = "requires_sponsorship"
        if semantic_type == "experience_years":
            skill = str((field.semantic_context or {}).get("skill", "")).strip()
            normalized_skill = _normalize(skill)
            for skill_key, skill_data in profile.skills.items():
                if not isinstance(skill_data, dict):
                    continue
                aliases = {_normalize(str(skill_key))}
                aliases.update(_normalize(str(tag)) for tag in skill_data.get("tags", []))
                if normalized_skill and normalized_skill in aliases:
                    years = skill_data.get("years")
                    if years is not None and str(years).strip():
                        value = str(years)
                        if not field.options or self._option_matches(value, field.options):
                            support_path = f"CareerProfile.skills.{skill_key}.years"
                            return self._field_answer(field, value, [support_path], "CareerProfile", 1.0, semantic_type)
            return None
        if semantic_type in _IDENTITY_SEMANTICS:
            identity_key = _IDENTITY_SEMANTICS[semantic_type]
            value = profile.identity.get(identity_key, "")
            if value:
                supported_by = [f"CareerProfile.identity.{identity_key}"]
                fact_id = profile.identity_fact_ids.get(identity_key)
                if fact_id:
                    supported_by.append(fact_id)
                if not field.options or self._option_matches(value, field.options):
                    return self._field_answer(field, value, supported_by, "CareerProfile", 1.0, semantic_type)
                return None
        if semantic_type == "current_company":
            # O Lever pergunta "Current company" (name="org").
            current = _current_experience(profile)
            if current and current.company:
                return self._field_answer(
                    field,
                    current.company,
                    [f"CareerProfile.experiences.{current.id}"],
                    "CareerProfile",
                    1.0,
                    semantic_type,
                )
            return None
        if semantic_type == "timezone" and preferences:
            timezone_value = preferences.timezone
            support_path = "CandidatePreferences.timezone"
            if not timezone_value and len(preferences.timezones) == 1:
                timezone_value = preferences.timezones[0]
                support_path = "CandidatePreferences.timezones[0]"
            if timezone_value:
                if not field.options or self._option_matches(timezone_value, field.options):
                    return self._field_answer(field, timezone_value, [support_path], "CandidatePreferences", 1.0, semantic_type)
                return None
        if semantic_type == "work_authorization" and preferences and preferences.work_authorization:
            country = _field_country(field)
            if not country:
                return None
            authorized_countries = {_normalize(item) for item in preferences.work_authorization}
            country_is_authorized = _normalize(country) in authorized_countries
            if self._yes_no_options(field.options):
                value = "Yes" if country_is_authorized else "No"
            elif field.field_type in {"select", "radio"} and field.options:
                values = {_normalize(option): option for option in field.options}
                value = next((values[_normalize(country)] for country in preferences.work_authorization if _normalize(country) in values), "") if country_is_authorized else ""
            else:
                value = country if country_is_authorized else ""
            if value:
                return self._field_answer(field, value, ["CandidatePreferences.work_authorization"], "CandidatePreferences", 1.0, semantic_type)
        if semantic_type == "requires_sponsorship" and preferences and preferences.requires_sponsorship in {"yes", "no"}:
            value = "Yes" if preferences.requires_sponsorship == "yes" else "No"
            value = self._map_boolean_option(value, field.options)
            return self._field_answer(field, value, ["CandidatePreferences.requires_sponsorship"], "CandidatePreferences", 1.0, semantic_type)
        if semantic_type == "relocation" and preferences:
            value = self._map_boolean_option("Yes" if preferences.relocation else "No", field.options)
            return self._field_answer(field, value, ["CandidatePreferences.relocation"], "CandidatePreferences", 1.0, semantic_type)
        answer = self.resolve(field.label, profile, preferences)
        if answer:
            answer = replace(answer, semantic_type=semantic_type, field_key=field.key)
        if answer and field.options and not self._option_matches(answer.answer, field.options):
            return None
        return answer

    @staticmethod
    def _yes_no_options(options: list[str]) -> bool:
        return {_normalize(option) for option in options} == {"yes", "no"}

    @staticmethod
    def _map_boolean_option(value: str, options: list[str]) -> str:
        """Map an abstract Yes/No answer to the exact ATS option label."""
        if not options or AnswerKnowledgeBase._yes_no_options(options):
            return value
        wanted = value == "Yes"
        for option in options:
            normalized = _normalize(option)
            is_negative = normalized.startswith("no ") or normalized == "no" or " do not " in f" {normalized} " or " not require " in f" {normalized} "
            if wanted and not is_negative and ("yes" in normalized or "require sponsorship" in normalized or "sponsor" in normalized):
                return option
            if not wanted and (is_negative or normalized.startswith("no ")):
                return option
        return value

    @staticmethod
    def _option_matches(value: str, options: list[str]) -> bool:
        normalized = _normalize(value)
        return any(normalized == _normalize(option) for option in options)

    @staticmethod
    def _field_answer(field: ApplicationField, value: str, supported_by: list[str], source: str, confidence: float, semantic_type: str) -> ApplicationAnswer:
        return ApplicationAnswer(question_key(field.label), field.label, value, supported_by, source, confidence, True, semantic_type in {"work_authorization", "requires_sponsorship"}, semantic_type, field.key)

    def _resolve_profile(self, question: str, profile: CareerProfile, preferences: CandidatePreferences | None) -> ApplicationAnswer | None:
        normalized = _normalize(question)
        if normalized in {"full name", "nome completo"}:
            value = profile.identity.get("name", "")
            if value:
                return ApplicationAnswer(question_key(question), question, value, ["CareerProfile.identity.name"], "CareerProfile", 1.0, True, False, "full_name")
        if "email" in normalized or "e mail" in normalized:
            value = profile.identity.get("email", "")
            if value:
                return ApplicationAnswer(question_key(question), question, value, ["CareerProfile.identity.email"], "CareerProfile", 1.0, True, False, "email")
        if preferences and any(term in normalized for term in ("work authorization", "work permit", "autorizacao de trabalho", "autorização de trabalho")):
            if preferences.work_authorization:
                return ApplicationAnswer(question_key(question), question, ", ".join(preferences.work_authorization), ["CandidatePreferences.work_authorization"], "CandidatePreferences", 1.0, True, True, "work_authorization")
        if preferences and any(term in normalized for term in ("remote", "remoto", "work from home")):
            return ApplicationAnswer(question_key(question), question, "Yes" if preferences.remote else "No", ["CandidatePreferences.remote"], "CandidatePreferences", 1.0, True, False, "remote")
        return None
