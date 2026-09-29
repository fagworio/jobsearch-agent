"""Modelos pequenos e serializáveis do motor de currículo V2."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResumeFact:
    fact_id: str
    fact_type: str
    statements: dict[str, str]
    tags: tuple[str, ...] = ()
    source: str = ""
    verified: bool = True

    def statement(self, language: str) -> str:
        return self.statements.get(language) or self.statements.get("en-US") or next(iter(self.statements.values()), "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResumeExperience:
    experience_id: str
    company: str
    role: str
    start_date: str
    end_date: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResumeEducation:
    education_id: str
    institution: str
    credential: dict[str, str]
    field_of_study: dict[str, str]
    start_date: str
    end_date: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResumeProfile:
    source: str
    source_hash: str
    identity: dict[str, str]
    summaries: dict[str, str]
    summary_fact_ids: dict[str, tuple[str, ...]]
    experiences: tuple[ResumeExperience, ...]
    education: tuple[ResumeEducation, ...]
    skills: tuple[tuple[str, tuple[str, ...]], ...]
    facts: dict[str, ResumeFact]
    max_pages: int = 2
    template: str = "ats"


@dataclass(frozen=True)
class ResumeStrategy:
    target_role: str
    language: str
    positioning: str
    focus: tuple[str, ...]
    secondary: tuple[str, ...]
    deprioritize: tuple[str, ...]
    keywords: tuple[str, ...]
    max_pages: int = 2
    template: str = "ats"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResumeClaim:
    claim: str
    supported_by: tuple[str, ...]
    valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResumeDocument:
    resume_id: str
    job_id: str
    company: str
    target_role: str
    language: str
    header: dict[str, str]
    summary: str
    skills: tuple[str, ...]
    experience: tuple[dict[str, Any], ...]
    education: tuple[dict[str, Any], ...]
    claims: tuple[ResumeClaim, ...]
    template: str = "ats"
    one_column: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["claims"] = [claim.to_dict() for claim in self.claims]
        return payload


@dataclass(frozen=True)
class ResumeValidation:
    valid: bool
    code: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "code": self.code,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metrics": self.metrics,
        }


@dataclass(frozen=True)
class ResumeBuild:
    job_id: str
    artifact_dir: str
    resume_path: str
    identity: str
    strategy: ResumeStrategy
    validation: ResumeValidation
    cache_hit: bool = False

    @property
    def ready(self) -> bool:
        return self.validation.valid and bool(self.resume_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "artifact_dir": self.artifact_dir,
            "resume_path": self.resume_path,
            "identity": self.identity,
            "strategy": self.strategy.to_dict(),
            "validation": self.validation.to_dict(),
            "cache_hit": self.cache_hit,
            "ready": self.ready,
        }
