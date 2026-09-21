"""Idioma, requisitos, fit e estratégia de posicionamento."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

from .llm import LLMError, LLMProvider, LLMRequest
from .models import CareerProfile, FitResult, Job, JobAnalysis, LanguageResult, ResumeStrategy
from .skills import SkillRegistry


PT_WORDS = {"com", "para", "você", "experiência", "desenvolvimento", "conhecimento", "vaga", "trabalho", "anos", "responsabilidades"}
EN_WORDS = {"with", "for", "you", "experience", "development", "knowledge", "job", "work", "years", "responsibilities"}

try:
    from lingua import Language as LinguaLanguage
    from lingua import LanguageDetectorBuilder
except ImportError:  # pragma: no cover - fallback do ambiente mínimo
    LinguaLanguage = None
    LanguageDetectorBuilder = None


@lru_cache(maxsize=1)
def skill_registry() -> SkillRegistry:
    return SkillRegistry.load()


@lru_cache(maxsize=1)
def language_detector():
    if not (LanguageDetectorBuilder and LinguaLanguage):
        return None
    return LanguageDetectorBuilder.from_languages(LinguaLanguage.PORTUGUESE, LinguaLanguage.ENGLISH).build()


def _normalized_language(value: str) -> tuple[str, str, str]:
    lowered = value.lower().replace("_", "-")
    if lowered.startswith("pt"):
        return "pt", "pt-BR", "BR"
    if lowered.startswith("en"):
        return "en", "en-US", "US"
    return value, value, ""


def detect_language(text: str, override: str | None = None) -> LanguageResult:
    if override:
        language, locale, country = _normalized_language(override)
        return LanguageResult(language, locale, country, 1.0, "override")
    detector = language_detector()
    if detector:
        try:
            detected = detector.detect_language_of(text)
            if detected is not None:
                language, locale, country = _normalized_language(detected.iso_code_639_1.name.lower())
                confidence = 0.75
                values = detector.compute_language_confidence_values(text)
                if values:
                    confidence = round(float(values[0].value), 3)
                return LanguageResult(language, locale, country, confidence, "lingua")
        except Exception:
            pass
    tokens = set(re.findall(r"[a-záéíóúãõç]+", text.lower()))
    pt = len(tokens & PT_WORDS)
    en = len(tokens & EN_WORDS)
    total = pt + en
    if total == 0:
        return LanguageResult("unknown", "unknown", "", 0.0, "deterministic")
    if pt >= en:
        return LanguageResult("pt", "pt-BR", "BR", round(pt / total, 3), "deterministic")
    return LanguageResult("en", "en-US", "US", round(en / total, 3), "deterministic")


def _terms(text: str) -> list[str]:
    return skill_registry().extract(text)


def _sentences(text: str) -> list[str]:
    return [part.strip(" -*•\t") for part in re.split(r"[\n.!?]+", text) if part.strip()]


def analyze_requirements(job: Job, provider: LLMProvider | None = None) -> JobAnalysis:
    description = job.description or ""
    language = detect_language(f"{job.title} {description}")
    skills = _terms(f"{job.title} {description}")
    sentences = _sentences(description)
    preferred = [skill for skill in skills if any("preferred" in sentence.lower() or "nice to have" in sentence.lower() or "bonus" in sentence.lower() for sentence in sentences if skill.lower() in sentence.lower())]
    required = [skill for skill in skills if skill not in preferred and re.search(rf"(required|must|strong|advanced|experience).*{re.escape(skill)}|{re.escape(skill)}.*(required|must|experience)", description, re.I)]
    required = required or [skill for skill in skills if skill not in preferred]
    years = ""
    years_match = re.search(r"(\d+\+?)\s+years?", description, re.I)
    if years_match:
        years = years_match.group(0)
    seniority = next((level for level in ("principal", "staff", "senior", "mid", "junior") if level in description.lower() or level in job.title.lower()), "unknown")
    result = JobAnalysis(
        language=language,
        required_skills=required,
        preferred_skills=preferred,
        years_of_experience=years,
        education=[sentence for sentence in sentences if "degree" in sentence.lower() or "formação" in sentence.lower()],
        language_requirements=[sentence for sentence in sentences if any(word in sentence.lower() for word in ("english", "inglês", "portuguese", "português"))],
        location_requirements=[job.location] if job.location else [],
        work_authorization="unknown",
        employment_type=job.employment_type,
        technologies=skills,
        responsibilities=sentences[:8],
        seniority=seniority,
        salary=job.salary,
        domain="software development" if skills else "unknown",
        explanation=["Requirements extracted deterministically from the normalized job description."],
    )
    if provider:
        try:
            enriched = provider.complete(LLMRequest(
                system="Return only grounded JSON requirements. Do not invent requirements.",
                user=f"Title: {job.title}\nDescription: {description}",
                schema="required_skills, preferred_skills, responsibilities, seniority, domain",
            ))
        except LLMError:
            raise
        if enriched:
            for field_name in ("required_skills", "preferred_skills", "responsibilities"):
                value = enriched.get(field_name)
                if isinstance(value, list) and all(isinstance(item, str) for item in value):
                    setattr(result, field_name, value)
            for field_name in ("seniority", "domain"):
                value = enriched.get(field_name)
                if isinstance(value, str) and value:
                    setattr(result, field_name, value)
            result.explanation.append("Semantic fields enriched by configured LLM provider.")
    return result


def _skill_keys(profile: CareerProfile) -> set[str]:
    keys = {key.lower() for key in profile.skills}
    for value in profile.skills.values():
        if isinstance(value, dict):
            keys.update(str(tag).lower() for tag in value.get("tags", []))
    return keys


def _normalize_term(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _profile_skill_values(profile: CareerProfile) -> list[str]:
    values = list(profile.skills)
    for value in profile.skills.values():
        if isinstance(value, dict):
            values.extend(str(tag) for tag in value.get("tags", []))
    return values


def _skill_matches(skill: str, profile_values: Iterable[str]) -> bool:
    return any(skill_registry().matches(skill, value) for value in profile_values)


LANGUAGE_LEVELS = {
    "basic": 0.3,
    "beginner": 0.3,
    "intermediate": 0.6,
    "advanced": 0.85,
    "fluent": 1.0,
    "native": 1.0,
}


def _profile_language_level(profile: CareerProfile, language: str) -> float:
    aliases = {"en": {"english", "en", "inglês", "ingles"}, "pt": {"portuguese", "pt", "português", "portugues"}}
    keys = aliases.get(language, set())
    for key, value in profile.languages.items():
        if key.lower() in keys:
            level = value.get("level", "") if isinstance(value, dict) else value
            return LANGUAGE_LEVELS.get(str(level).lower(), 0.0)
    return 0.0


def _required_language_level(analysis: JobAnalysis, language: str) -> float:
    text = " ".join(analysis.language_requirements).lower()
    if language == "en" and not text:
        # A vaga predominantemente em inglês exige capacidade operacional, mas
        # não presume fluência nativa sem declarar isso.
        return 0.6
    for label, score in (("native", 1.0), ("fluent", 1.0), ("advanced", 0.85), ("intermediate", 0.6), ("basic", 0.3)):
        if label in text or (language == "en" and label in text) or (language == "pt" and label in text):
            return score
    return 0.6 if text else 0.5


def _language_fit(analysis: JobAnalysis, profile: CareerProfile) -> tuple[float, bool]:
    language = analysis.language.language
    if language not in {"en", "pt"}:
        return 0.5, False
    candidate = _profile_language_level(profile, language)
    required = _required_language_level(analysis, language)
    if candidate <= 0:
        return 0.0, True
    return round(min(1.0, candidate / required), 3), candidate < required * 0.75


def _location_fit(job: Job, profile: CareerProfile) -> tuple[float, bool]:
    preferences = profile.preferences
    location = f"{job.remote_type} {job.location}".lower()
    is_remote = any(term in location for term in ("remote", "remoto", "distributed", "work from home"))
    if is_remote:
        return (1.0 if preferences.get("remote", False) else 0.5), False
    allowed = [str(item).lower() for item in preferences.get("allowed_locations", []) if item]
    job_location = job.location.lower()
    if not job_location:
        return 0.5, False
    if any(item in job_location or job_location in item for item in allowed):
        return 1.0, False
    if preferences.get("relocation", False):
        return 0.6, False
    # Vaga presencial/híbrida fora das localidades permitidas.
    if any(term in location for term in ("on-site", "onsite", "presencial", "hybrid", "híbrido")):
        return 0.25, True
    if preferences.get("remote", False):
        return 0.25, True
    return 0.5, False


def _ratio(found: Iterable[str], wanted: Iterable[str]) -> float:
    wanted_set = {item.lower() for item in wanted}
    return 1.0 if not wanted_set else len({item.lower() for item in found} & wanted_set) / len(wanted_set)


def calculate_fit(job: Job, analysis: JobAnalysis, profile: CareerProfile) -> FitResult:
    profile_skills = _skill_keys(profile)
    required = analysis.required_skills
    preferred = analysis.preferred_skills
    matched = [skill for skill in required + preferred if _skill_matches(skill, profile_skills)]
    missing_required = [skill for skill in required if skill not in matched]
    missing_preferred = [skill for skill in preferred if skill not in matched]
    required_match = _ratio(matched, required)
    preferred_match = _ratio(matched, preferred)
    experience_match = 1.0 if profile.experiences else 0.0
    language_match, language_blocker = _language_fit(analysis, profile)
    location_match, location_blocker = _location_fit(job, profile)
    blockers = []
    if analysis.work_authorization == "unknown":
        blockers.append("work_authorization_unknown")
    if language_blocker:
        blockers.append("language_mismatch")
    if location_blocker:
        blockers.append("location_mismatch")
    score = round(100 * (0.45 * required_match + 0.15 * preferred_match + 0.15 * experience_match + 0.15 * language_match + 0.10 * location_match), 2)
    explanation = [f"Required coverage: {required_match:.0%}.", f"Preferred coverage: {preferred_match:.0%}.", f"Language match: {language_match:.0%}.", f"Location match: {location_match:.0%}."]
    if missing_required:
        explanation.append("Missing required skills: " + ", ".join(missing_required))
    return FitResult(score, required_match, preferred_match, experience_match, language_match, location_match, matched, missing_required, missing_preferred, blockers, explanation)


def build_strategy(job: Job, analysis: JobAnalysis, fit: FitResult, profile: CareerProfile) -> ResumeStrategy:
    profile_values = _profile_skill_values(profile)
    focus = [skill for skill in analysis.required_skills if _skill_matches(skill, profile_values)]
    secondary = [skill for skill in analysis.preferred_skills if _skill_matches(skill, profile_values)]
    deprioritize = [key for key in profile.skills if not _skill_matches(key, focus + secondary)]
    language = analysis.language.locale if analysis.language.locale in {"pt-BR", "en-US"} else "en-US"
    return ResumeStrategy(job.title or "Target role", language, f"Position candidate around {job.title} expertise.", focus, secondary, deprioritize, (focus + secondary)[:12])
