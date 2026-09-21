"""Contratos de domínio serializáveis e independentes de Hermes/browser."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobState(StrEnum):
    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    ANALYZED = "ANALYZED"
    SCORED = "SCORED"
    QUALIFIED = "QUALIFIED"
    RESUME_STRATEGY_READY = "RESUME_STRATEGY_READY"
    RESUME_READY = "RESUME_READY"
    NEEDS_HUMAN = "NEEDS_HUMAN"


@dataclass
class Job:
    id: str
    source: str
    external_id: str
    company: str
    title: str
    description: str
    location: str = ""
    country: str = ""
    remote_type: str = "unknown"
    employment_type: str = "unknown"
    salary: str = ""
    currency: str = ""
    language: str = "unknown"
    url: str = ""
    posted_at: str = ""
    discovered_at: str = field(default_factory=now_iso)
    requirements: list[str] = field(default_factory=list)
    preferred_requirements: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    state: JobState = JobState.DISCOVERED


@dataclass
class Fact:
    id: str
    type: str
    statements: dict[str, str]
    tags: list[str] = field(default_factory=list)
    source: str = ""
    verified: bool = True


@dataclass
class Experience:
    id: str
    company: str
    role: str
    start_date: str
    end_date: str = ""
    fact_ids: list[str] = field(default_factory=list)


@dataclass
class CareerProfile:
    identity: dict[str, str]
    professional_summary: dict[str, str]
    experiences: list[Experience]
    skills: dict[str, dict[str, Any]]
    languages: dict[str, dict[str, Any]]
    preferences: dict[str, Any] = field(default_factory=dict)
    demo: bool = False
    version: str = "1"


@dataclass
class LanguageResult:
    language: str
    locale: str
    country: str
    confidence: float
    method: str


@dataclass
class JobAnalysis:
    language: LanguageResult
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    years_of_experience: str = ""
    education: list[str] = field(default_factory=list)
    language_requirements: list[str] = field(default_factory=list)
    location_requirements: list[str] = field(default_factory=list)
    work_authorization: str = "unknown"
    employment_type: str = "unknown"
    technologies: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    seniority: str = "unknown"
    salary: str = ""
    domain: str = ""
    explanation: list[str] = field(default_factory=list)


@dataclass
class FitResult:
    score: float
    required_match: float
    preferred_match: float
    experience_match: float
    language_match: float
    location_match: float
    matched_skills: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_preferred: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)


@dataclass
class ResumeStrategy:
    target_role: str
    language: str
    positioning: str
    focus: list[str]
    secondary: list[str]
    deprioritize: list[str]
    keywords: list[str]
    max_pages: int = 2
    template: str = "ats"


@dataclass
class ResumeClaim:
    claim: str
    supported_by: list[str]
    valid: bool = True


@dataclass
class Resume:
    id: str
    job_id: str
    language: str
    header: dict[str, str]
    summary: str
    skills: list[str]
    experience: list[dict[str, Any]]
    education: list[dict[str, Any]] = field(default_factory=list)
    claims: list[ResumeClaim] = field(default_factory=list)
    template: str = "ats"


@dataclass
class ValidationResult:
    valid: bool
    code: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def to_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return {key: to_dict(item) for key, item in value.model_dump(mode="json").items()}
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_dict(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(item) for item in value]
    return value
