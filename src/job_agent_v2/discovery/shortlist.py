"""Ranking determinístico e justificável das vagas descobertas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .eligibility import assess_geography
from .models import DiscoveryMatrix, DiscoveryMatch


@dataclass(frozen=True)
class ShortlistEntry:
    rank: int
    job_id: str
    url: str
    title: str
    company: str
    shortlist_score: float
    match_score: float
    match_band: str
    geography_status: str
    geography_scope: str
    selection: str
    fit_decision: str
    application_readiness: str
    missing_facts: tuple[str, ...]
    applied: bool
    matched_skills: tuple[str, ...]
    matched_roles: tuple[str, ...]
    queries: tuple[str, ...]
    justification: tuple[str, ...]
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "job_id": self.job_id,
            "url": self.url,
            "title": self.title,
            "company": self.company,
            "shortlist_score": self.shortlist_score,
            "match_score": self.match_score,
            "match_band": self.match_band,
            "geography_status": self.geography_status,
            "geography_scope": self.geography_scope,
            "selection": self.selection,
            "fit_decision": self.fit_decision,
            "application_readiness": self.application_readiness,
            "missing_facts": list(self.missing_facts),
            "applied": self.applied,
            "matched_skills": list(self.matched_skills),
            "matched_roles": list(self.matched_roles),
            "queries": list(self.queries),
            "justification": list(self.justification),
            "description": self.description,
        }


@dataclass(frozen=True)
class ShortlistReport:
    provider: str
    source: str
    min_match_score: float
    auto_approve_score: float
    entries: tuple[ShortlistEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.selection] = counts.get(entry.selection, 0) + 1
        return {
            "provider": self.provider,
            "source": self.source,
            "policy": {
                "min_match_score": self.min_match_score,
                "auto_approve_score": self.auto_approve_score,
                "auto_approve_bands": ["STRONG", "GOOD"],
                "ineligible_or_applied": "REJECTED",
                "unknown_geography": "UNKNOWN",
            },
            "counts": counts,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _match_by_id(matrix: DiscoveryMatrix) -> dict[str, DiscoveryMatch]:
    return {match.job_id: match for match in matrix.matches}


def rank_shortlist(
    matrix: DiscoveryMatrix,
    *,
    source: str = "data/v2-discovery/matrix.json",
    min_match_score: float = 30.0,
    auto_approve_score: float = 40.0,
) -> ShortlistReport:
    matches = _match_by_id(matrix)
    if len(matches) != len(matrix.unique_jobs):
        raise ValueError("matrix must contain matching results for every unique job")

    pending: list[ShortlistEntry] = []
    for occurrence in matrix.unique_jobs:
        job = occurrence.job
        match = matches[job.job_id]
        geography = assess_geography(job)
        geography_score = {
            "ELIGIBLE": 100.0,
            "LIKELY_ELIGIBLE": 80.0,
            "UNKNOWN": 50.0,
            "INELIGIBLE": 0.0,
        }[geography.status.value]
        shortlist_score = round(match.score * 0.8 + geography_score * 0.2, 1)
        reasons = list(match.evidence)
        reasons.append(geography.evidence)
        if job.applied:
            selection = "REJECTED"
            fit_decision = "REJECTED"
            application_readiness = "ALREADY_APPLIED"
            reasons.append("application status on card indicates the candidate already applied")
        elif geography.status.value == "INELIGIBLE":
            selection = "REJECTED"
            fit_decision = "REJECTED"
            application_readiness = "UNSUPPORTED"
        elif match.score < min_match_score:
            selection = "REJECTED"
            fit_decision = "REJECTED"
            application_readiness = "UNSUPPORTED"
            reasons.append(f"match score {match.score:.1f} is below the minimum {min_match_score:.1f}")
        elif not match.role_compatible:
            selection = "REJECTED"
            fit_decision = "REJECTED"
            application_readiness = "UNSUPPORTED"
            reasons.append("title is outside the configured technical role families")
        elif (
            geography.status.value in {"ELIGIBLE", "LIKELY_ELIGIBLE"}
            and match.score >= auto_approve_score
            and match.band in {"STRONG", "GOOD", "PARTIAL"}
        ):
            selection = "APPROVED"
            fit_decision = "APPROVED"
            application_readiness = "PENDING_INSPECTION"
            reasons.append("meets the automatic shortlist threshold")
        else:
            selection = "REVIEW"
            fit_decision = "UNKNOWN"
            application_readiness = "UNSUPPORTED"
            reasons.append("requires review because geography or confidence is not conclusive")
        pending.append(ShortlistEntry(
            rank=0,
            job_id=job.job_id,
            url=job.href,
            title=job.title,
            company=job.company,
            shortlist_score=shortlist_score,
            match_score=match.score,
            match_band=match.band,
            geography_status=geography.status.value,
            geography_scope=geography.scope,
            selection=selection,
            fit_decision=fit_decision,
            application_readiness=application_readiness,
            missing_facts=(),
            applied=job.applied,
            matched_skills=match.matched_skills,
            matched_roles=match.matched_roles,
            queries=occurrence.queries,
            justification=tuple(dict.fromkeys(reasons)),
            description=job.description,
        ))

    ordered = sorted(pending, key=lambda item: (-item.shortlist_score, -item.match_score, item.job_id))
    ranked = tuple(
        ShortlistEntry(
            rank=index,
            job_id=entry.job_id,
            url=entry.url,
            title=entry.title,
            company=entry.company,
            shortlist_score=entry.shortlist_score,
            match_score=entry.match_score,
            match_band=entry.match_band,
            geography_status=entry.geography_status,
            geography_scope=entry.geography_scope,
            selection=entry.selection,
            fit_decision=entry.fit_decision,
            application_readiness=entry.application_readiness,
            missing_facts=entry.missing_facts,
            applied=entry.applied,
            matched_skills=entry.matched_skills,
            matched_roles=entry.matched_roles,
            queries=entry.queries,
            justification=entry.justification,
            description=entry.description,
        )
        for index, entry in enumerate(ordered, 1)
    )
    return ShortlistReport(matrix.provider, source, min_match_score, auto_approve_score, ranked)


def save_shortlist(report: ShortlistReport, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination
