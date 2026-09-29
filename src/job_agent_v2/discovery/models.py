"""Modelos neutros do primeiro incremento de descoberta."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveryJob:
    provider: str
    job_id: str
    title: str
    company: str
    href: str
    remote: bool
    work_type: str
    location: str
    salary: str | None
    posted: str
    status: str
    applied: bool
    viewed: bool
    # Texto descritivo observado no card de resultados. A página de busca não
    # expõe o HTML completo da vaga, portanto este campo permanece
    # explicitamente parcial até a inspeção da vaga aberta.
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiscoveryResults:
    provider: str
    page_type: str
    surface: str
    url: str
    title: str
    ready: bool
    query: str
    work_type: tuple[str, ...]
    jobs: tuple[DiscoveryJob, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "page_type": self.page_type,
            "surface": self.surface,
            "url": self.url,
            "title": self.title,
            "ready": self.ready,
            "query": self.query,
            "work_type": list(self.work_type),
            "jobs": [job.to_dict() for job in self.jobs],
        }


@dataclass(frozen=True)
class DiscoveryFilterOption:
    label: str
    value: str
    selected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "selected": self.selected,
        }


@dataclass(frozen=True)
class DiscoveryFilter:
    key: str
    label: str
    control: str
    parameter: str
    selected: tuple[str, ...]
    options: tuple[DiscoveryFilterOption, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "control": self.control,
            "parameter": self.parameter,
            "selected": list(self.selected),
            "options": [option.to_dict() for option in self.options],
        }


@dataclass(frozen=True)
class DiscoveryFilters:
    provider: str
    page_type: str
    surface: str
    url: str
    title: str
    ready: bool
    query: str
    parameters: dict[str, tuple[str, ...]]
    filters: tuple[DiscoveryFilter, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "page_type": self.page_type,
            "surface": self.surface,
            "url": self.url,
            "title": self.title,
            "ready": self.ready,
            "query": self.query,
            "parameters": {key: list(values) for key, values in self.parameters.items()},
            "filters": [filter_snapshot.to_dict() for filter_snapshot in self.filters],
        }


@dataclass(frozen=True)
class DiscoverySearchRun:
    family: str
    query: str
    results: DiscoveryResults

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "query": self.query,
            "results": self.results.to_dict(),
        }


@dataclass(frozen=True)
class DiscoveryJobOccurrence:
    """Representação canônica de uma vaga e suas origens na matriz."""

    job: DiscoveryJob
    families: tuple[str, ...]
    queries: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        from .eligibility import assess_geography

        payload = {
            "job": self.job.to_dict(),
            "families": list(self.families),
            "queries": list(self.queries),
        }
        payload["geography"] = assess_geography(self.job).to_dict()
        return payload


@dataclass(frozen=True)
class DiscoveryMatch:
    job_id: str
    score: float
    band: str
    matched_skills: tuple[str, ...]
    matched_roles: tuple[str, ...]
    query_signals: tuple[str, ...]
    evidence: tuple[str, ...]
    basis: str
    role_compatible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "score": self.score,
            "band": self.band,
            "matched_skills": list(self.matched_skills),
            "matched_roles": list(self.matched_roles),
            "query_signals": list(self.query_signals),
            "evidence": list(self.evidence),
            "basis": self.basis,
            "role_compatible": self.role_compatible,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DiscoveryMatch":
        list_fields = ("matched_skills", "matched_roles", "query_signals", "evidence")
        if not isinstance(value.get("job_id"), str) or not isinstance(value.get("score"), (int, float)):
            raise ValueError("matching job_id and score are required")
        if not isinstance(value.get("band"), str) or not isinstance(value.get("basis"), str):
            raise ValueError("matching band and basis are required")
        for field_name in list_fields:
            values = value.get(field_name)
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"matching {field_name} must be a string list")
        return cls(
            job_id=value["job_id"],
            score=float(value["score"]),
            band=value["band"],
            matched_skills=tuple(value["matched_skills"]),
            matched_roles=tuple(value["matched_roles"]),
            query_signals=tuple(value["query_signals"]),
            evidence=tuple(value["evidence"]),
            basis=value["basis"],
            role_compatible=bool(value.get("role_compatible", True)),
        )


def deduplicate_runs(runs: tuple[DiscoverySearchRun, ...] | list[DiscoverySearchRun]) -> tuple[DiscoveryJobOccurrence, ...]:
    """Deduplica vagas por ``job_id`` preservando a ordem da primeira ocorrência."""

    grouped: dict[str, tuple[DiscoveryJob, list[str], list[str]]] = {}
    for run in runs:
        for job in run.results.jobs:
            if not job.job_id:
                raise ValueError("cannot deduplicate a job without job_id")
            current = grouped.get(job.job_id)
            if current is None:
                grouped[job.job_id] = (job, [run.family], [run.query])
                continue
            _first_job, families, queries = current
            if run.family not in families:
                families.append(run.family)
            if run.query not in queries:
                queries.append(run.query)

    return tuple(
        DiscoveryJobOccurrence(job, tuple(families), tuple(queries))
        for job, families, queries in grouped.values()
    )


@dataclass(frozen=True)
class DiscoveryMatrix:
    provider: str
    work_type: tuple[str, ...]
    runs: tuple[DiscoverySearchRun, ...]
    matches: tuple[DiscoveryMatch, ...] = ()
    match_profile: str = ""

    @property
    def raw_job_count(self) -> int:
        return sum(len(run.results.jobs) for run in self.runs)

    @property
    def unique_jobs(self) -> tuple[DiscoveryJobOccurrence, ...]:
        return deduplicate_runs(self.runs)

    def to_dict(self) -> dict[str, Any]:
        unique_jobs = self.unique_jobs
        geography_counts: dict[str, int] = {}
        deduplicated_jobs: list[dict[str, Any]] = []
        matches_by_id = {match.job_id: match for match in self.matches}
        for occurrence in unique_jobs:
            job_payload = occurrence.to_dict()
            status = job_payload["geography"]["status"]
            geography_counts[status] = geography_counts.get(status, 0) + 1
            match = matches_by_id.get(occurrence.job.job_id)
            if match is not None:
                job_payload["match"] = match.to_dict()
            deduplicated_jobs.append(job_payload)
        payload: dict[str, Any] = {
            "provider": self.provider,
            "work_type": list(self.work_type),
            "query_count": len(self.runs),
            "job_count": self.raw_job_count,
            "unique_job_count": len(unique_jobs),
            "duplicate_job_count": self.raw_job_count - len(unique_jobs),
            "geography": {
                "candidate_scope": "Brazil/LATAM/Worldwide",
                "counts": geography_counts,
            },
            "runs": [run.to_dict() for run in self.runs],
            "deduplicated_jobs": deduplicated_jobs,
        }
        if self.matches:
            match_counts: dict[str, int] = {}
            for match in self.matches:
                match_counts[match.band] = match_counts.get(match.band, 0) + 1
            payload["matching"] = {
                "profile": self.match_profile,
                "basis": "job card title, observed card description, and search-query provenance",
                "counts_by_band": match_counts,
                "matches": [match.to_dict() for match in self.matches],
            }
        return payload
