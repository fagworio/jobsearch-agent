"""Schemas Pydantic para fronteiras externas, com fallback no modo mínimo."""

from __future__ import annotations

from typing import Any

PYDANTIC_AVAILABLE = False
try:
    from pydantic import BaseModel, ConfigDict, Field

    PYDANTIC_AVAILABLE = True

    class ExternalJobSchema(BaseModel):
        model_config = ConfigDict(extra="allow")
        title: str | None = None
        description: str | None = None
        id: str | int | None = None

    class FactSchema(BaseModel):
        model_config = ConfigDict(extra="allow")
        type: str = "general"
        statement: dict[str, str] = Field(default_factory=dict)
        tags: list[str] = Field(default_factory=list)
        verified: bool = True

    class ProfileSchema(BaseModel):
        model_config = ConfigDict(extra="allow")
        identity: dict[str, str]
        professional_summary: dict[str, str] = Field(default_factory=dict)
        experience: list[dict[str, Any]] = Field(default_factory=list)
        skills: dict[str, Any] = Field(default_factory=dict)
        languages: dict[str, Any] = Field(default_factory=dict)

    class LanguageResultSchema(BaseModel):
        language: str
        locale: str
        country: str = ""
        confidence: float = 0.0
        method: str = ""

    class JobSchema(BaseModel):
        model_config = ConfigDict(extra="allow")
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
        discovered_at: str = ""
        requirements: list[str] = Field(default_factory=list)
        preferred_requirements: list[str] = Field(default_factory=list)
        raw_payload: dict[str, Any] = Field(default_factory=dict)
        state: str = "DISCOVERED"

    class JobAnalysisSchema(BaseModel):
        language: LanguageResultSchema
        required_skills: list[str] = Field(default_factory=list)
        preferred_skills: list[str] = Field(default_factory=list)
        years_of_experience: str = ""
        education: list[str] = Field(default_factory=list)
        language_requirements: list[str] = Field(default_factory=list)
        location_requirements: list[str] = Field(default_factory=list)
        work_authorization: str = "unknown"
        employment_type: str = "unknown"
        technologies: list[str] = Field(default_factory=list)
        responsibilities: list[str] = Field(default_factory=list)
        seniority: str = "unknown"
        salary: str = ""
        domain: str = ""
        explanation: list[str] = Field(default_factory=list)

    class FitResultSchema(BaseModel):
        score: float
        required_match: float
        preferred_match: float
        experience_match: float
        language_match: float
        location_match: float
        matched_skills: list[str] = Field(default_factory=list)
        missing_required: list[str] = Field(default_factory=list)
        missing_preferred: list[str] = Field(default_factory=list)
        blockers: list[str] = Field(default_factory=list)
        explanation: list[str] = Field(default_factory=list)

    class ResumeStrategySchema(BaseModel):
        target_role: str
        language: str
        positioning: str
        focus: list[str] = Field(default_factory=list)
        secondary: list[str] = Field(default_factory=list)
        deprioritize: list[str] = Field(default_factory=list)
        keywords: list[str] = Field(default_factory=list)
        max_pages: int = 2
        template: str = "ats"

    class ResumeSchema(BaseModel):
        id: str
        job_id: str
        language: str
        header: dict[str, str]
        summary: str
        skills: list[str] = Field(default_factory=list)
        experience: list[dict[str, Any]] = Field(default_factory=list)
        education: list[dict[str, Any]] = Field(default_factory=list)
        claims: list[dict[str, Any]] = Field(default_factory=list)
        template: str = "ats"

except ImportError:  # pragma: no cover - fallback do ambiente mínimo
    class ExternalJobSchema:  # type: ignore[no-redef]
        @classmethod
        def model_validate(cls, value: dict[str, Any]) -> dict[str, Any]:
            return value

    class FactSchema:  # type: ignore[no-redef]
        @classmethod
        def model_validate(cls, value: dict[str, Any]) -> dict[str, Any]:
            return value

    class ProfileSchema:  # type: ignore[no-redef]
        @classmethod
        def model_validate(cls, value: dict[str, Any]) -> dict[str, Any]:
            return value

    class LanguageResultSchema:  # type: ignore[no-redef]
        @classmethod
        def model_validate(cls, value: dict[str, Any]) -> dict[str, Any]:
            return value

    class JobSchema:  # type: ignore[no-redef]
        @classmethod
        def model_validate(cls, value: dict[str, Any]) -> dict[str, Any]:
            return value

    class JobAnalysisSchema(JobSchema):  # type: ignore[no-redef]
        pass

    class FitResultSchema(JobSchema):  # type: ignore[no-redef]
        pass

    class ResumeStrategySchema(JobSchema):  # type: ignore[no-redef]
        pass

    class ResumeSchema(JobSchema):  # type: ignore[no-redef]
        pass


def validate_external_job(payload: dict[str, Any]) -> dict[str, Any]:
    model = ExternalJobSchema.model_validate(payload)
    return model if isinstance(model, dict) else model.model_dump(mode="python")


def validate_contract(name: str, value: Any) -> Any:
    contract = {
        "job": JobSchema,
        "analysis": JobAnalysisSchema,
        "fit": FitResultSchema,
        "strategy": ResumeStrategySchema,
        "resume": ResumeSchema,
    }.get(name)
    if contract is None:
        raise ValueError(f"unknown contract: {name}")
    if isinstance(value, dict):
        payload = value
    elif hasattr(value, "model_dump"):
        payload = value.model_dump(mode="python")
    else:
        from .models import to_dict
        payload = to_dict(value)
    return contract.model_validate(payload)
