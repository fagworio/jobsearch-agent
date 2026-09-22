"""Loader e validação do Career Profile e dos locked facts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import CandidatePreferences, CareerProfile, Experience, Fact
from .schemas import FactSchema, ProfileSchema


class ProfileError(ValueError):
    pass


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{label} must be a mapping")
    return value


def load_profile(path: str | Path) -> CareerProfile:
    source = Path(path)
    data = _mapping(yaml.safe_load(source.read_text(encoding="utf-8")) or {}, "profile")
    ProfileSchema.model_validate(data)
    identity = _mapping(data.get("identity"), "identity")
    summary = _mapping(data.get("professional_summary", {}), "professional_summary")
    summary_text: dict[str, str] = {}
    summary_fact_ids: dict[str, list[str]] = {}
    for language, value in summary.items():
        if isinstance(value, dict):
            summary_text[str(language)] = str(value.get("text", ""))
            summary_fact_ids[str(language)] = [str(item) for item in value.get("fact_ids", [])]
        else:
            summary_text[str(language)] = str(value)
    legacy_summary_ids = data.get("professional_summary_fact_ids", {})
    if isinstance(legacy_summary_ids, dict):
        summary_fact_ids.update({str(k): [str(item) for item in v] for k, v in legacy_summary_ids.items() if isinstance(v, list)})
    raw_experiences = data.get("experience", [])
    if not isinstance(raw_experiences, list):
        raise ProfileError("experience must be a list")
    experiences: list[Experience] = []
    for item in raw_experiences:
        row = _mapping(item, "experience item")
        experiences.append(
            Experience(
                id=str(row.get("id", "")),
                company=str(row.get("company", "")),
                role=str(row.get("role", "")),
                start_date=str(row.get("start_date", "")),
                end_date=str(row.get("end_date") or ""),
                fact_ids=[str(item) for item in row.get("facts", [])],
            )
        )
    profile = CareerProfile(
        identity={str(k): str(v) for k, v in identity.items()},
        professional_summary=summary_text,
        summary_fact_ids=summary_fact_ids,
        experiences=experiences,
        skills=_mapping(data.get("skills", {}), "skills"),
        languages=_mapping(data.get("languages", {}), "languages"),
        preferences=_mapping(data.get("preferences", {}), "preferences"),
        demo=bool(data.get("demo", False)),
        version=str(data.get("version", "1")),
    )
    validate_profile(profile)
    return profile


def load_preferences(path: str | Path, legacy: dict[str, Any] | None = None) -> CandidatePreferences:
    """Load mutable candidate preferences, with explicit legacy compatibility."""
    source = Path(path)
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) if source.exists() else {}
    data = _mapping(raw or {}, "preferences")
    legacy_data = dict(legacy or {})
    merged: dict[str, Any] = dict(legacy_data)
    merged.update(data)
    resume = _mapping(merged.get("resume", {}), "preferences.resume")
    compensation = _mapping(merged.get("compensation", {}), "preferences.compensation")
    locations = _mapping(merged.get("locations", {}), "preferences.locations")
    limits = _mapping(merged.get("limits", {}), "preferences.limits")
    autonomy = _mapping(merged.get("autonomy", {}), "preferences.autonomy")
    def preference_value(name: str, nested: dict[str, Any], default: Any = None) -> Any:
        if name in data:
            return data[name]
        if name in nested:
            return nested[name]
        return legacy_data.get(name, default)

    return CandidatePreferences(
        remote=bool(preference_value("remote", locations, False)),
        allowed_locations=[str(item) for item in preference_value("allowed_locations", locations, []) if item],
        allowed_countries=[str(item) for item in preference_value("allowed_countries", locations, []) if item],
        relocation=bool(preference_value("relocation", locations, False)),
        minimum_salary=str(preference_value("minimum_salary", compensation, "")),
        currency=str(preference_value("currency", compensation, "USD" if compensation.get("minimum_monthly_usd") else "")),
        employment_types=[str(item) for item in preference_value("employment_types", {}, []) if item],
        work_authorization=[str(item) for item in preference_value("work_authorization", {}, []) if item],
        timezones=[str(item) for item in preference_value("timezones", {}, []) if item],
        max_applications_per_day=int(preference_value("max_applications_per_day", limits)) if preference_value("max_applications_per_day", limits) is not None else None,
        resume_template=str(preference_value("resume_template", resume, "ats")),
        max_pages=int(preference_value("max_pages", resume, 2)),
        autonomy={str(k): str(v) for k, v in autonomy.items()},
    )


def load_facts(path: str | Path) -> dict[str, Fact]:
    source = Path(path)
    data = _mapping(yaml.safe_load(source.read_text(encoding="utf-8")) or {}, "facts")
    raw_facts = _mapping(data.get("facts"), "facts.facts")
    result: dict[str, Fact] = {}
    for fact_id, raw in raw_facts.items():
        row = _mapping(raw, f"fact {fact_id}")
        FactSchema.model_validate(row)
        statements = _mapping(row.get("statement"), f"fact {fact_id}.statement")
        result[str(fact_id)] = Fact(
            id=str(fact_id),
            type=str(row.get("type", "general")),
            statements={str(k): str(v) for k, v in statements.items()},
            tags=[str(tag).lower() for tag in row.get("tags", [])],
            source=str(row.get("source", "")),
            verified=bool(row.get("verified", True)),
        )
    if not result:
        raise ProfileError("at least one fact is required")
    return result


def validate_profile(profile: CareerProfile) -> None:
    if not profile.identity.get("name"):
        raise ProfileError("identity.name is required")
    for experience in profile.experiences:
        if not experience.id or not experience.role:
            raise ProfileError("each experience requires id and role")
    if profile.demo and not profile.identity.get("name"):
        raise ProfileError("demo profile still requires an identity name")


def validate_facts(profile: CareerProfile, facts: dict[str, Fact]) -> list[str]:
    errors: list[str] = []
    referenced = {fact_id for exp in profile.experiences for fact_id in exp.fact_ids}
    missing = sorted(referenced - facts.keys())
    if missing:
        errors.append(f"experience references unknown facts: {', '.join(missing)}")
    for fact in facts.values():
        if not fact.verified:
            errors.append(f"fact is not verified: {fact.id}")
        if not fact.statements:
            errors.append(f"fact has no statements: {fact.id}")
    return errors
