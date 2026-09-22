"""Contratos de domínio serializáveis e independentes de Hermes/browser."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

try:
    from pydantic.dataclasses import dataclass as domain_dataclass
except ImportError:  # pragma: no cover - modo mínimo sem dependências instaladas
    domain_dataclass = dataclass


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


class IdentityStrength(StrEnum):
    STRONG = "strong"
    WEAK = "weak"


class DuplicateStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED_DUPLICATE = "confirmed_duplicate"
    CONFIRMED_DISTINCT = "confirmed_distinct"


class FitCriterionStatus(StrEnum):
    MATCH = "match"
    PARTIAL = "partial"
    MISSING = "missing"
    BLOCKER = "blocker"
    UNKNOWN = "unknown"


@domain_dataclass
class WorkAuthorizationRequirement:
    required: bool = False
    countries: list[str] = field(default_factory=list)
    sponsorship_available: str = "unknown"
    evidence: str = ""
    source: str = "deterministic"


class ApplicationState(StrEnum):
    DRAFT = "DRAFT"
    PREPARING = "PREPARING"
    MATERIALS_READY = "MATERIALS_READY"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    READY_TO_APPLY = "READY_TO_APPLY"
    NEEDS_ANSWER = "NEEDS_ANSWER"
    NEEDS_ARTIFACT = "NEEDS_ARTIFACT"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    NEEDS_MFA = "NEEDS_MFA"
    NEEDS_CAPTCHA = "NEEDS_CAPTCHA"
    UNSUPPORTED_FORM = "UNSUPPORTED_FORM"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    REJECTED = "REJECTED"


@domain_dataclass
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


@domain_dataclass
class Fact:
    id: str
    type: str
    statements: dict[str, str]
    tags: list[str] = field(default_factory=list)
    source: str = ""
    verified: bool = True


@domain_dataclass
class Experience:
    id: str
    company: str
    role: str
    start_date: str
    end_date: str = ""
    fact_ids: list[str] = field(default_factory=list)


@domain_dataclass
class CareerProfile:
    identity: dict[str, str]
    professional_summary: dict[str, str]
    experiences: list[Experience]
    skills: dict[str, dict[str, Any]]
    languages: dict[str, dict[str, Any]]
    preferences: dict[str, Any] = field(default_factory=dict)
    candidate_preferences: "CandidatePreferences | None" = None
    summary_fact_ids: dict[str, list[str]] = field(default_factory=dict)
    demo: bool = False
    version: str = "1"


@domain_dataclass
class LanguageResult:
    language: str
    locale: str
    country: str
    confidence: float
    method: str


@domain_dataclass
class LanguageRequirement:
    language: str
    minimum_level: str = "intermediate"
    required: bool = True
    evidence: str = ""
    source: str = "deterministic"


@domain_dataclass
class CandidatePreferences:
    remote: bool = False
    allowed_locations: list[str] = field(default_factory=list)
    allowed_countries: list[str] = field(default_factory=list)
    relocation: bool = False
    minimum_salary: str = ""
    currency: str = ""
    employment_types: list[str] = field(default_factory=list)
    work_authorization: list[str] = field(default_factory=list)
    requires_sponsorship: str = "unknown"
    timezones: list[str] = field(default_factory=list)
    max_applications_per_day: int | None = None
    resume_template: str = "ats"
    max_pages: int = 2
    autonomy: dict[str, str] = field(default_factory=dict)


@domain_dataclass
class JobAnalysis:
    language: LanguageResult
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    years_of_experience: str = ""
    education: list[str] = field(default_factory=list)
    language_requirements: list[LanguageRequirement] = field(default_factory=list)
    location_requirements: list[str] = field(default_factory=list)
    work_authorization: str = "unknown"
    work_authorization_requirement: WorkAuthorizationRequirement = field(default_factory=WorkAuthorizationRequirement)
    employment_type: str = "unknown"
    technologies: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    seniority: str = "unknown"
    salary: str = ""
    domain: str = ""
    explanation: list[str] = field(default_factory=list)


@domain_dataclass
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
    criteria: list["FitCriterionResult"] = field(default_factory=list)


@domain_dataclass
class FitCriterionResult:
    criterion: str
    candidate_value: Any = ""
    requirement: Any = ""
    result: FitCriterionStatus = FitCriterionStatus.UNKNOWN
    score: float = 0.0
    blocker: bool = False
    evidence: str = ""
    source: str = ""


@domain_dataclass
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


@domain_dataclass
class ResumeClaim:
    claim: str
    supported_by: list[str]
    valid: bool = True


@domain_dataclass
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


@domain_dataclass
class ValidationResult:
    valid: bool
    code: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@domain_dataclass
class Application:
    id: str
    job_id: str
    state: ApplicationState = ApplicationState.DRAFT
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@domain_dataclass
class ApplicationEvent:
    application_id: str
    from_state: ApplicationState
    to_state: ApplicationState
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)


@domain_dataclass
class ApplicationAnswer:
    question_key: str
    question: str
    answer: str = ""
    supported_by: list[str] = field(default_factory=list)
    source: str = "unknown"
    confidence: float = 0.0
    approved: bool = False
    legal: bool = False
    semantic_type: str = "unknown"


@domain_dataclass
class ApplicationReadiness:
    decision: ApplicationState
    ready_to_apply: bool
    requires_review: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)


@domain_dataclass
class ApplicationPolicy:
    autonomy: dict[str, str] = field(default_factory=lambda: {
        "search": "auto",
        "analyze": "auto",
        "generate_resume": "auto",
        "answer_known_questions": "auto",
        "fill_forms": "review",
        "submit": "manual",
    })
    applications_per_day: int = 20
    unknown_answer: str = "stop"
    captcha: str = "stop"
    mfa: str = "stop"
    legal_question: str = "stop"


@domain_dataclass
class ApplicationField:
    key: str
    label: str
    field_type: str = "text"
    semantic_type: str = "unknown"
    required: bool = False
    options: list[str] = field(default_factory=list)
    value: Any = ""
    answer: ApplicationAnswer | None = None
    confidence: float = 0.0
    source: str = "unknown"
    step: str = ""
    attachment_path: str = ""
    accepted_types: list[str] = field(default_factory=list)
    multiple: bool = False
    disabled: bool = False


@domain_dataclass
class ApplicationForm:
    form_id: str
    provider: str = "generic"
    fields: list[ApplicationField] = field(default_factory=list)
    source: str = "fixture"
    steps: list[str] = field(default_factory=list)
    artifact_root: str = ""


@domain_dataclass
class ApplicationContext:
    application_id: str
    job_id: str
    fit: dict[str, Any] = field(default_factory=dict)
    resume: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    answers: list[ApplicationAnswer] = field(default_factory=list)
    form: ApplicationForm | None = None
    policy: ApplicationPolicy = field(default_factory=ApplicationPolicy)


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
