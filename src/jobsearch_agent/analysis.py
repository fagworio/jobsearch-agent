"""Idioma, requisitos, fit e estratégia de posicionamento."""

from __future__ import annotations

import re
from typing import Iterable

from .llm import LLMError, LLMProvider, LLMRequest
from .models import CareerProfile, FitResult, Job, JobAnalysis, LanguageResult, ResumeStrategy


PT_WORDS = {"com", "para", "você", "experiência", "desenvolvimento", "conhecimento", "vaga", "trabalho", "anos", "responsabilidades"}
EN_WORDS = {"with", "for", "you", "experience", "development", "knowledge", "job", "work", "years", "responsibilities"}


def detect_language(text: str, override: str | None = None) -> LanguageResult:
    if override:
        locale = "pt-BR" if override.lower().startswith("pt") else "en-US" if override.lower().startswith("en") else override
        return LanguageResult(override, locale, "BR" if locale == "pt-BR" else "US", 1.0, "override")
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
    known = [
        "WordPress", "WooCommerce", "PHP", "Python", "JavaScript", "TypeScript", "React", "Gutenberg",
        "Docker", "REST API", "GraphQL", "Git", "SQL", "Shopify", "AWS", "Laravel", "Node.js",
    ]
    lowered = text.lower()
    return [term for term in known if term.lower() in lowered]


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


def _ratio(found: Iterable[str], wanted: Iterable[str]) -> float:
    wanted_set = {item.lower() for item in wanted}
    return 1.0 if not wanted_set else len({item.lower() for item in found} & wanted_set) / len(wanted_set)


def calculate_fit(job: Job, analysis: JobAnalysis, profile: CareerProfile) -> FitResult:
    profile_skills = _skill_keys(profile)
    required = analysis.required_skills
    preferred = analysis.preferred_skills
    matched = [skill for skill in required + preferred if _normalize_term(skill) in {_normalize_term(item) for item in profile_skills} or any(_normalize_term(skill) in _normalize_term(tag) for tag in profile_skills)]
    missing_required = [skill for skill in required if skill not in matched]
    missing_preferred = [skill for skill in preferred if skill not in matched]
    required_match = _ratio(matched, required)
    preferred_match = _ratio(matched, preferred)
    experience_match = 1.0 if profile.experiences else 0.0
    language_match = 1.0 if analysis.language.language in {"pt", "en"} else 0.5
    location_match = 1.0 if not job.location or bool(profile.preferences.get("remote", False)) else 0.5
    blockers = []
    if analysis.work_authorization == "unknown":
        blockers.append("work_authorization_unknown")
    score = round(100 * (0.45 * required_match + 0.15 * preferred_match + 0.15 * experience_match + 0.15 * language_match + 0.10 * location_match), 2)
    explanation = [f"Required coverage: {required_match:.0%}.", f"Preferred coverage: {preferred_match:.0%}."]
    if missing_required:
        explanation.append("Missing required skills: " + ", ".join(missing_required))
    return FitResult(score, required_match, preferred_match, experience_match, language_match, location_match, matched, missing_required, missing_preferred, blockers, explanation)


def build_strategy(job: Job, analysis: JobAnalysis, fit: FitResult, profile: CareerProfile) -> ResumeStrategy:
    skills = analysis.required_skills + [skill for skill in analysis.preferred_skills if skill in fit.matched_skills]
    profile_keys = {key.lower() for key in profile.skills}
    focus = [skill for skill in skills if _normalize_term(skill) in {_normalize_term(item) for item in profile_keys} or any(_normalize_term(skill) in _normalize_term(tag) for tag in profile_keys)]
    deprioritize = [key for key in profile.skills if key.lower() not in {item.lower() for item in focus}]
    language = analysis.language.locale if analysis.language.locale in {"pt-BR", "en-US"} else "en-US"
    return ResumeStrategy(job.title or "Target role", language, f"Position candidate around {job.title} expertise.", focus, deprioritize[:3], deprioritize[3:], focus[:12])
