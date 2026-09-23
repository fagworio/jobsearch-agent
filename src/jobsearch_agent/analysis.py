"""Idioma, requisitos, fit e estratégia de posicionamento."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

from .llm import LLMError, LLMProvider, LLMRequest
from .models import CareerProfile, FitCriterionResult, FitCriterionStatus, FitResult, Job, JobAnalysis, LanguageRequirement, LanguageResult, ResumeStrategy, WorkAuthorizationRequirement
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


def _language_name(value: str) -> str:
    lowered = value.lower()
    if lowered in {"english", "inglês", "ingles", "en"}:
        return "en"
    if lowered in {"portuguese", "português", "portugues", "pt"}:
        return "pt"
    return lowered


def _extract_language_requirements(sentences: list[str]) -> list[LanguageRequirement]:
    requirements: list[LanguageRequirement] = []
    language_terms = ("english", "inglês", "ingles", "portuguese", "português", "portugues")
    levels = {
        "native": "native", "nativo": "native", "nativa": "native",
        "fluent": "fluent", "fluente": "fluent",
        "advanced": "advanced", "avançado": "advanced", "avancado": "advanced",
        "intermediate": "intermediate", "intermediário": "intermediate", "intermediario": "intermediate",
        "basic": "basic", "básico": "basic", "basico": "basic",
        "beginner": "beginner", "iniciante": "beginner",
    }
    for sentence in sentences:
        lowered = sentence.lower()
        term = next((item for item in language_terms if item in lowered), None)
        if not term:
            continue
        level = next((normalized for label, normalized in levels.items() if re.search(rf"\b{re.escape(label)}\b", lowered)), "intermediate")
        required = bool(re.search(r"\b(required|must|mandatory|obrig\w*|necess\w*|fluency|proficien\w*)\b", lowered))
        requirements.append(LanguageRequirement(_language_name(term), level, required, sentence, "deterministic"))
    return requirements


def _extract_work_authorization_requirement(description: str) -> WorkAuthorizationRequirement:
    sentences = _sentences(description)
    terms = ("authorized to work", "legally authorized", "work authorization", "work permit", "visa sponsorship", "patrocínio de visto", "autorização de trabalho", "autorizacao de trabalho")
    matching_sentences = [item for item in sentences if any(term in item.lower() for term in terms)]
    sentence = " ".join(matching_sentences)
    if not sentence:
        return WorkAuthorizationRequirement()
    lowered = sentence.lower()
    countries: list[str] = []
    country_aliases = {
        "us": ("us", "u.s.", "united states", "estados unidos"),
        "brazil": ("brazil", "brasil"),
        "canada": ("canada",),
        "uk": ("uk", "united kingdom", "reino unido"),
    }
    for country, aliases in country_aliases.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            countries.append(country)
    if re.search(r"\b(no|not|without|does not|doesn't)\b[^.]*\b(sponsor|patroc)", lowered):
        sponsorship = "not_available"
    elif re.search(r"\b(offer|available|provides|provide)\b[^.]*\b(sponsor|patroc)", lowered):
        sponsorship = "available"
    else:
        sponsorship = "unknown"
    return WorkAuthorizationRequirement(True, countries, sponsorship, sentence, "deterministic")


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
        language_requirements=_extract_language_requirements(sentences),
        location_requirements=[job.location] if job.location else [],
        work_authorization="unknown",
        work_authorization_requirement=_extract_work_authorization_requirement(description),
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
    text = " ".join(requirement.evidence for requirement in analysis.language_requirements if requirement.language == language).lower()
    if language == "en" and not text:
        # A vaga predominantemente em inglês exige capacidade operacional, mas
        # não presume fluência nativa sem declarar isso.
        return 0.6
    for label, score in (("native", 1.0), ("fluent", 1.0), ("advanced", 0.85), ("intermediate", 0.6), ("basic", 0.3)):
        if label in text or (language == "en" and label in text) or (language == "pt" and label in text):
            return score
    return 0.6 if text else 0.5


def _language_fit(analysis: JobAnalysis, profile: CareerProfile) -> tuple[float, bool, list[FitCriterionResult]]:
    language = analysis.language.language
    criteria: list[FitCriterionResult] = []
    if language not in {"en", "pt"}:
        return 0.5, False, [FitCriterionResult("primary_language", "unknown", "unknown", FitCriterionStatus.UNKNOWN, 0.5, False, "Language detection was inconclusive.", "language_detector")]

    checks: list[tuple[str, float, float, bool, str]] = [(language, _profile_language_level(profile, language), _required_language_level(analysis, language), True, f"Primary job language: {analysis.language.locale}")]
    checks.extend((requirement.language, _profile_language_level(profile, requirement.language), LANGUAGE_LEVELS.get(requirement.minimum_level.lower(), 0.6), requirement.required, requirement.evidence) for requirement in analysis.language_requirements)
    values: list[float] = []
    blockers = False
    for required_language, candidate, required, required_flag, evidence in checks:
        value = 0.0 if candidate <= 0 else round(min(1.0, candidate / max(required, 0.01)), 3)
        values.append(value)
        is_blocker = required_flag and candidate < required
        blockers = blockers or is_blocker
        status = FitCriterionStatus.BLOCKER if is_blocker else FitCriterionStatus.MATCH if value >= 1.0 else FitCriterionStatus.PARTIAL
        criteria.append(FitCriterionResult(f"language:{required_language}", candidate, required, status, value, is_blocker, evidence, "profile + job_analysis"))
    return round(min(values) if values else 0.5, 3), blockers, criteria


def _location_fit(job: Job, profile: CareerProfile) -> tuple[float, bool]:
    preferences = profile.candidate_preferences or profile.preferences
    if hasattr(preferences, "remote"):
        preferences = {
            "remote": preferences.remote,
            "allowed_locations": preferences.allowed_locations,
            "allowed_countries": preferences.allowed_countries,
            "relocation": preferences.relocation,
        }
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


def _work_authorization_fit(analysis: JobAnalysis, profile: CareerProfile) -> tuple[float, str, FitCriterionResult]:
    requirement = analysis.work_authorization_requirement
    preferences = profile.candidate_preferences or profile.preferences
    candidate_values = preferences.work_authorization if hasattr(preferences, "work_authorization") else preferences.get("work_authorization", [])
    requires_sponsorship = preferences.requires_sponsorship if hasattr(preferences, "requires_sponsorship") else preferences.get("requires_sponsorship", "unknown")
    candidate_values = [str(value).lower() for value in candidate_values]
    candidate_countries = set(candidate_values)
    if any(value in candidate_countries for value in ("united states", "u.s.", "estados unidos", "us")):
        candidate_countries.add("us")
    if any(value in candidate_countries for value in ("brazil", "brasil")):
        candidate_countries.add("brazil")
    if any(value in candidate_countries for value in ("united kingdom", "reino unido", "uk")):
        candidate_countries.add("uk")
    if not requirement.required:
        return 1.0, "", FitCriterionResult("work_authorization", candidate_values, "not required by job", FitCriterionStatus.UNKNOWN, 1.0, False, "The job has no explicit work authorization requirement.", "job_analysis")
    if not candidate_values:
        return 0.0, "work_authorization_unknown", FitCriterionResult("work_authorization", [], requirement.countries, FitCriterionStatus.UNKNOWN, 0.0, True, requirement.evidence, "job_analysis + candidate_preferences")
    if requirement.sponsorship_available == "not_available" and str(requires_sponsorship).lower() == "yes":
        return 0.0, "sponsorship_unavailable", FitCriterionResult("sponsorship", requires_sponsorship, requirement.sponsorship_available, FitCriterionStatus.BLOCKER, 0.0, True, requirement.evidence, "job_analysis + candidate_preferences")
    if not requirement.countries or candidate_countries.intersection(requirement.countries):
        return 1.0, "", FitCriterionResult("work_authorization", candidate_values, requirement.countries, FitCriterionStatus.MATCH, 1.0, False, requirement.evidence, "job_analysis + candidate_preferences")
    return 0.0, "work_authorization_mismatch", FitCriterionResult("work_authorization", candidate_values, requirement.countries, FitCriterionStatus.BLOCKER, 0.0, True, requirement.evidence, "job_analysis + candidate_preferences")


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
    requirements_known = bool(required)
    # Absence of evidence is not evidence of a match: a posting whose
    # requirements could not be extracted must not score as a perfect fit.
    required_match = _ratio(matched, required) if requirements_known else 0.0
    preferred_match = _ratio(matched, preferred)
    experience_match = 1.0 if profile.experiences else 0.0
    language_match, language_blocker, language_criteria = _language_fit(analysis, profile)
    location_match, location_blocker = _location_fit(job, profile)
    blockers = []
    work_authorization_match, work_authorization_blocker, work_authorization_criterion = _work_authorization_fit(analysis, profile)
    if work_authorization_blocker:
        blockers.append(work_authorization_blocker)
    if language_blocker:
        blockers.append("language_mismatch")
    if location_blocker:
        blockers.append("location_mismatch")
    blockers.extend(f"missing_required:{skill}" for skill in missing_required)
    criteria: list[FitCriterionResult] = []
    criteria.extend(
        FitCriterionResult(
            f"required_skill:{skill}",
            "matched" if skill in matched else "missing",
            skill,
            FitCriterionStatus.MATCH if skill in matched else FitCriterionStatus.BLOCKER,
            1.0 if skill in matched else 0.0,
            skill in missing_required,
            "Candidate skill registry and job requirements",
            "skill_registry",
        )
        for skill in required
    )
    criteria.extend(
        FitCriterionResult(
            f"preferred_skill:{skill}",
            "matched" if skill in matched else "missing",
            skill,
            FitCriterionStatus.MATCH if skill in matched else FitCriterionStatus.PARTIAL,
            1.0 if skill in matched else 0.0,
            False,
            "Candidate skill registry and job preferences",
            "skill_registry",
        )
        for skill in preferred
    )
    criteria.extend(language_criteria)
    criteria.append(work_authorization_criterion)
    location_result = FitCriterionStatus.BLOCKER if location_blocker else FitCriterionStatus.MATCH if location_match >= 1 else FitCriterionStatus.PARTIAL
    preferences = profile.candidate_preferences or profile.preferences
    criteria.append(FitCriterionResult("location", preferences, job.location or job.remote_type, location_result, location_match, location_blocker, f"Job location: {job.location or job.remote_type}", "job + candidate_preferences"))
    criteria.append(FitCriterionResult("experience", len(profile.experiences), analysis.years_of_experience or "experience", FitCriterionStatus.MATCH if experience_match else FitCriterionStatus.MISSING, experience_match, False, "Profile experience records", "career_profile"))
    if not requirements_known:
        criteria.append(FitCriterionResult("required_skills", 0, "at least one recognised requirement", FitCriterionStatus.UNKNOWN, 0.0, False, "No required skill was extracted from the posting; required coverage is reported as 0%", "skill_registry"))
    score = round(100 * (0.42 * required_match + 0.14 * preferred_match + 0.14 * experience_match + 0.15 * language_match + 0.05 * location_match + 0.10 * work_authorization_match), 2)
    explanation = [f"Required coverage: {required_match:.0%}.", f"Preferred coverage: {preferred_match:.0%}.", f"Language match: {language_match:.0%}.", f"Location match: {location_match:.0%}."]
    if not requirements_known:
        explanation.append("No required skill was extracted from the posting, so fit is not estimated.")
    if missing_required:
        explanation.append("Missing required skills: " + ", ".join(missing_required))
    return FitResult(score, required_match, preferred_match, experience_match, language_match, location_match, matched, missing_required, missing_preferred, blockers, explanation, criteria)


def build_strategy(job: Job, analysis: JobAnalysis, fit: FitResult, profile: CareerProfile) -> ResumeStrategy:
    profile_values = _profile_skill_values(profile)
    focus = [skill for skill in analysis.required_skills if _skill_matches(skill, profile_values)]
    secondary = [skill for skill in analysis.preferred_skills if _skill_matches(skill, profile_values)]
    deprioritize = [key for key in profile.skills if not _skill_matches(key, focus + secondary)]
    language = analysis.language.locale if analysis.language.locale in {"pt-BR", "en-US"} else "en-US"
    return ResumeStrategy(job.title or "Target role", language, f"Position candidate around {job.title} expertise.", focus, secondary, deprioritize, (focus + secondary)[:12])
