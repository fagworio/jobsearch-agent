"""Deterministic, provenance-aware question and answer resolution."""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from .models import ApplicationAnswer, CandidatePreferences, CareerProfile


LEGAL_TERMS = ("legal", "law", "declaration", "declare", "conviction", "criminal", "accommod", "disability", "ethnicity", "race", "visto", "autorização de trabalho", "autorizacao de trabalho")


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks.lower()).strip()


def question_key(question: str) -> str:
    normalized = _normalize(question)
    return "q-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def is_legal_question(question: str) -> bool:
    normalized = _normalize(question)
    return any(term in normalized for term in LEGAL_TERMS)


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
        ))
    return result


class AnswerKnowledgeBase:
    def __init__(self, answers: list[ApplicationAnswer]):
        self.answers = [answer for answer in answers if answer.approved and answer.answer]

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

    def _resolve_profile(self, question: str, profile: CareerProfile, preferences: CandidatePreferences | None) -> ApplicationAnswer | None:
        normalized = _normalize(question)
        if any(term in normalized for term in ("full name", "name", "nome")):
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
