"""Matching determinístico entre cards descobertos e o Career Profile local."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .models import DiscoveryJobOccurrence, DiscoveryMatch, DiscoveryMatrix


@dataclass(frozen=True)
class MatchProfile:
    source: str
    skills: tuple[tuple[str, tuple[str, ...]], ...]
    role_terms: tuple[str, ...]


_ROLE_FAMILY_TERMS = (
    "wordpress",
    "woocommerce",
    "shopify",
    "front end",
    "frontend",
    "web",
    "php",
    "cms",
    "full stack",
    "software engineer",
    "application developer",
)
_ROLE_FAMILY_EXCLUSIONS = (
    "project manager",
    "program manager",
    "product manager",
    "marketing",
    "growth manager",
    "sales",
    "recruiter",
    "talent acquisition",
    "account manager",
    "customer success",
    "business development",
)


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9+#]+", " ", without_marks).strip()


def _contains(text: str, alias: str) -> bool:
    normalized = _normalise(alias)
    if not normalized:
        return False
    # Short aliases such as "go" and "in" are too ambiguous to identify a
    # skill from a job title. JS and TS are retained because they are standard
    # front-end abbreviations and are matched as whole tokens.
    if len(normalized.replace(" ", "")) < 3 and normalized not in {"js", "ts"}:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None


def _display_name(key: str) -> str:
    return {
        "nextjs": "Next.js",
        "nodejs": "Node.js",
        "rest_api": "REST API",
        "tailwind_css": "Tailwind CSS",
    }.get(key, key.replace("_", " ").title())


def _profile_role_terms(data: dict[str, Any]) -> tuple[str, ...]:
    roles: list[str] = []
    for item in data.get("experience", []):
        if isinstance(item, dict):
            roles.append(str(item.get("role", "")))
    role_text = _normalise(" | ".join(roles))
    candidates = ("wordpress", "shopify", "front end", "frontend", "web", "ux ui", "ecommerce")
    return tuple(term for term in candidates if _contains(role_text, term))


def role_family_compatible(title: str) -> bool:
    """Reject adjacent job families before description skills can overmatch."""

    normalized = _normalise(title)
    if any(_contains(normalized, term) for term in _ROLE_FAMILY_EXCLUSIONS):
        return False
    return any(_contains(normalized, term) for term in _ROLE_FAMILY_TERMS)


def load_match_profile(path: str | Path) -> MatchProfile:
    source = Path(path)
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), dict):
        raise ValueError("match profile must contain a skills mapping")
    skills: list[tuple[str, tuple[str, ...]]] = []
    for key, raw in payload["skills"].items():
        key_text = str(key)
        aliases = {key_text.replace("_", " ")}
        if isinstance(raw, dict):
            raw_tags = raw.get("tags", [])
            if isinstance(raw_tags, list):
                aliases.update(str(tag) for tag in raw_tags)
        valid_aliases = tuple(dict.fromkeys(alias for alias in aliases if _normalise(alias)))
        skills.append((_display_name(key_text), valid_aliases))
    return MatchProfile(str(source), tuple(skills), _profile_role_terms(payload))


def match_occurrence(occurrence: DiscoveryJobOccurrence, profile: MatchProfile) -> DiscoveryMatch:
    title = _normalise(occurrence.job.title)
    description = _normalise(occurrence.job.description)
    query_text = _normalise(" | ".join(occurrence.queries))
    title_skills: list[str] = []
    description_skills: list[str] = []
    query_skills: list[str] = []
    for label, aliases in profile.skills:
        if any(_contains(title, alias) for alias in aliases):
            title_skills.append(label)
        elif any(_contains(description, alias) for alias in aliases):
            description_skills.append(label)
        elif any(_contains(query_text, alias) for alias in aliases):
            query_skills.append(label)

    matched_roles = tuple(term for term in profile.role_terms if _contains(title, term))
    role_compatible = role_family_compatible(occurrence.job.title)
    generic_role = any(
        _contains(title, term)
        for term in ("developer", "engineer", "software", "web", "frontend", "front end", "architect", "designer")
    )
    title_points = min(66, len(title_skills) * 22)
    description_points = min(24, len(description_skills) * 8)
    role_points = min(22, len(matched_roles) * 22)
    generic_points = 10 if generic_role else 0
    query_points = min(15, len(query_skills) * 5)
    score = round(float(min(100, title_points + description_points + role_points + generic_points + query_points)), 1)
    if score >= 70:
        band = "STRONG"
    elif score >= 50:
        band = "GOOD"
    elif score >= 30:
        band = "PARTIAL"
    elif score >= 15:
        band = "WEAK"
    else:
        band = "INSUFFICIENT"

    evidence: list[str] = []
    if title_skills:
        evidence.append(f"title explicitly matches profile skills: {', '.join(title_skills)}")
    if description_skills:
        evidence.append(f"job description mentions profile skills: {', '.join(description_skills)}")
    if matched_roles:
        evidence.append(f"title aligns with profile roles: {', '.join(matched_roles)}")
    if query_skills:
        evidence.append(f"search provenance matches profile skills: {', '.join(query_skills)}")
    if not evidence:
        evidence.append("no explicit profile skill or role signal in the observed title/query")
    if not role_compatible:
        evidence.append("title is outside the configured technical role families")
    return DiscoveryMatch(
        job_id=occurrence.job.job_id,
        score=score,
        band=band,
        matched_skills=tuple(title_skills + description_skills + query_skills),
        matched_roles=matched_roles,
        query_signals=occurrence.queries,
        evidence=tuple(evidence),
        basis="job card title, observed card description, and search-query provenance",
        role_compatible=role_compatible,
    )


def match_matrix(matrix: DiscoveryMatrix, profile: MatchProfile) -> DiscoveryMatrix:
    matches = tuple(match_occurrence(occurrence, profile) for occurrence in matrix.unique_jobs)
    return replace(matrix, matches=matches, match_profile=profile.source)
