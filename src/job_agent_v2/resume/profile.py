"""Leitura explícita do Career Profile para o motor de currículo V2."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from .models import ResumeEducation, ResumeExperience, ResumeFact, ResumeProfile


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text_map(value: object) -> dict[str, str]:
    return {str(key): str(item) for key, item in _mapping(value).items() if str(item).strip()}


def load_profile(path: str | Path, facts_path: str | Path | None = None) -> ResumeProfile:
    source = Path(path)
    raw_text = source.read_text(encoding="utf-8")
    payload = yaml.safe_load(raw_text) or {}
    if not isinstance(payload, dict):
        raise ValueError("resume profile must contain a YAML object")

    identity = {str(key): str(value) for key, value in _mapping(payload.get("identity")).items()}
    raw_summaries = _mapping(payload.get("professional_summary"))
    summaries: dict[str, str] = {}
    summary_fact_ids: dict[str, tuple[str, ...]] = {}
    for language, value in raw_summaries.items():
        data = _mapping(value)
        summaries[str(language)] = str(data.get("text") if data else value)
        ids = data.get("fact_ids", []) if data else []
        summary_fact_ids[str(language)] = tuple(str(item) for item in ids if str(item).strip())

    experiences: list[ResumeExperience] = []
    for item in payload.get("experience", []):
        data = _mapping(item)
        facts = data.get("facts", data.get("fact_ids", []))
        experiences.append(ResumeExperience(
            experience_id=str(data.get("id", "")),
            company=str(data.get("company", "")),
            role=str(data.get("role", "")),
            start_date=str(data.get("start_date", "")),
            end_date=str(data.get("end_date", "")),
            fact_ids=tuple(str(value) for value in facts if str(value).strip()) if isinstance(facts, list) else (),
        ))

    education: list[ResumeEducation] = []
    for item in payload.get("education", []):
        data = _mapping(item)
        facts = data.get("facts", data.get("fact_ids", []))
        education.append(ResumeEducation(
            education_id=str(data.get("id", "")),
            institution=str(data.get("institution", "")),
            credential=_text_map(data.get("credential")),
            field_of_study=_text_map(data.get("field_of_study")),
            start_date=str(data.get("start_date", "")),
            end_date=str(data.get("end_date", "")),
            fact_ids=tuple(str(value) for value in facts if str(value).strip()) if isinstance(facts, list) else (),
        ))

    skills: list[tuple[str, tuple[str, ...]]] = []
    for key, value in _mapping(payload.get("skills")).items():
        data = _mapping(value)
        tags = data.get("tags", [])
        aliases = tuple(dict.fromkeys([str(key).replace("_", " ")] + [str(tag) for tag in tags])) if isinstance(tags, list) else (str(key),)
        skills.append((str(key), aliases))

    facts_payload = _mapping(payload.get("facts"))
    facts_source = Path(facts_path) if facts_path else None
    if not facts_payload and facts_source is None:
        for candidate in (source.with_name("locked_facts.local.yaml"), source.with_name("locked_facts.yaml")):
            if candidate.exists():
                facts_source = candidate
                break
    if not facts_payload and facts_source is not None and facts_source.exists():
        external = yaml.safe_load(facts_source.read_text(encoding="utf-8")) or {}
        facts_payload = _mapping(external.get("facts")) if isinstance(external, dict) else {}

    facts: dict[str, ResumeFact] = {}
    for fact_id, value in facts_payload.items():
        data = _mapping(value)
        statements = _text_map(data.get("statement", data.get("statements")))
        if not statements:
            continue
        tags = data.get("tags", [])
        facts[str(fact_id)] = ResumeFact(
            fact_id=str(fact_id),
            fact_type=str(data.get("type", "")),
            statements=statements,
            tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (),
            source=str(data.get("source", "")),
            verified=data.get("verified") is not False,
        )

    preferences = _mapping(payload.get("preferences"))
    max_pages = int(preferences.get("max_pages", 2) or 2)
    return ResumeProfile(
        source=str(source),
        source_hash=hashlib.sha256((raw_text + json_text(facts_source)).encode("utf-8")).hexdigest(),
        identity=identity,
        summaries=summaries,
        summary_fact_ids=summary_fact_ids,
        experiences=tuple(experiences),
        education=tuple(education),
        skills=tuple(skills),
        facts=facts,
        max_pages=max_pages,
        template=str(preferences.get("resume_template", "ats")),
    )


def json_text(path: Path | None) -> str:
    """Return external fact bytes for cache identity without exposing contents."""
    return path.read_text(encoding="utf-8") if path is not None and path.exists() else ""
