"""Seleção explicável de vagas aderentes ao perfil.

O fit sozinho não decide: um anúncio onde o extrator reconheceu apenas uma
skill produz cobertura alta e um score enganoso. A seleção exige, além do
score, evidência suficiente de que o anúncio foi entendido e de que a vaga
pertence à área de atuação do candidato. Toda rejeição carrega um motivo
auditável.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import CareerProfile, Job

#: Disciplinas que não são a área de atuação do candidato. Um título que casa
#: aqui é recusado mesmo com score alto, salvo se também casar com um termo alvo.
DEFAULT_EXCLUDE_TERMS: tuple[str, ...] = (
    "backend",
    "back-end",
    "back end",
    "devops",
    "sre",
    "site reliability",
    "database",
    "data engineer",
    "data scientist",
    "machine learning",
    "ml engineer",
    "ai engineer",
    "ios",
    "android",
    "mobile engineer",
    "security engineer",
    "penetration",
    "sales",
    "account executive",
    "marketing",
    "recruiter",
    "talent",
    "finance",
    "legal",
    "support engineer",
)

#: Níveis abaixo do candidato. Sempre recusam, mesmo quando o título casa com um
#: termo alvo: "Frontend Engineer Intern" contém "frontend".
DEFAULT_SENIORITY_EXCLUDE_TERMS: tuple[str, ...] = (
    "intern",
    "internship",
    "internships",
    "junior",
    "jr",
    "entry level",
    "entry-level",
    "graduate",
    "new grad",
    "apprentice",
    "trainee",
    "working student",
    "summer 20",
    "spring 20",
)

#: Onde o candidato aceita trabalhar. Fora disto a vaga é recusada quando
#: ``allow_location_mismatch`` é falso.
DEFAULT_LOCATION_TERMS: tuple[str, ...] = (
    "remote",
    "brazil",
    "brasil",
    "latam",
    "latin america",
    "south america",
    "americas",
    "anywhere",
    "worldwide",
    "global",
    "distributed",
)

#: Termos que indicam a área do candidato (web/front-end/WordPress/Shopify).
DEFAULT_TARGET_TERMS: tuple[str, ...] = (
    "frontend",
    "front-end",
    "front end",
    "react",
    "next.js",
    "nextjs",
    "vue",
    "angular",
    "javascript",
    "typescript",
    "wordpress",
    "woocommerce",
    "shopify",
    "web developer",
    "web engineer",
    "full stack",
    "fullstack",
    "ui engineer",
    "ui developer",
    "cms",
    "liquid",
)


@dataclass(frozen=True)
class ShortlistCriteria:
    min_required_skills: int = 3
    min_score: float = 70.0
    max_per_company: int = 1
    target_terms: tuple[str, ...] = DEFAULT_TARGET_TERMS
    exclude_terms: tuple[str, ...] = DEFAULT_EXCLUDE_TERMS
    seniority_exclude_terms: tuple[str, ...] = DEFAULT_SENIORITY_EXCLUDE_TERMS
    location_terms: tuple[str, ...] = DEFAULT_LOCATION_TERMS
    allow_location_mismatch: bool = False


@dataclass
class ShortlistEntry:
    job: Job
    score: float
    required_skills: list[str]
    matched_skills: list[str]
    decision: str
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.decision == "SHORTLISTED"


@dataclass
class ShortlistResult:
    accepted: list[ShortlistEntry] = field(default_factory=list)
    rejected: list[ShortlistEntry] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return len(self.accepted) + len(self.rejected)


def _contains(haystack: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack) is not None


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if _contains(text, term)]


def terms_from_profile(profile: CareerProfile) -> tuple[str, ...]:
    """Derive target role terms from the candidate's own experience titles."""
    derived: list[str] = []
    for experience in profile.experiences:
        for token in re.split(r"[|/,]", experience.role or ""):
            cleaned = token.strip().casefold()
            if len(cleaned) >= 4:
                derived.append(cleaned)
    return tuple(dict.fromkeys((*DEFAULT_TARGET_TERMS, *derived)))


def evaluate_job(
    job: Job,
    score: float,
    required_skills: list[str],
    matched_skills: list[str],
    blockers: list[str],
    criteria: ShortlistCriteria,
) -> ShortlistEntry:
    title = (job.title or "").casefold()
    entry = ShortlistEntry(job, score, list(required_skills), list(matched_skills), "REJECTED")

    # 1. O anúncio precisa ter sido entendido antes de qualquer julgamento.
    if len(required_skills) < criteria.min_required_skills:
        entry.reason = f"INSUFFICIENT_REQUIREMENTS: {len(required_skills)} extraída(s), mínimo {criteria.min_required_skills}"
        return entry

    # 2. Nível abaixo do candidato recusa sempre, mesmo com título aderente.
    low = _matched_terms(title, criteria.seniority_exclude_terms)
    if low:
        entry.reason = "SENIORITY_MISMATCH: " + ", ".join(low)
        return entry

    # 3. A vaga precisa estar onde o candidato aceita trabalhar.
    if not criteria.allow_location_mismatch:
        where = f"{job.location} {job.remote_type}".casefold()
        if not _matched_terms(where, criteria.location_terms):
            entry.reason = f"LOCATION_UNSUPPORTED: {job.location or job.remote_type or 'não informado'}"
            return entry

    # 4. A vaga precisa pertencer à área de atuação do candidato.
    targets = _matched_terms(title, criteria.target_terms)
    excluded = _matched_terms(title, criteria.exclude_terms)
    if excluded and not targets:
        entry.reason = "OFF_TARGET_ROLE: " + ", ".join(excluded)
        return entry

    # 5. Bloqueios de fit (idioma, autorização, skill obrigatória).
    hard = [item for item in blockers if item not in {"work_authorization_unknown", "location_mismatch"}]
    if hard:
        entry.reason = "FIT_BLOCKED: " + ", ".join(hard)
        return entry

    # 6. Só então o score decide.
    if score < criteria.min_score:
        entry.reason = f"BELOW_SCORE: {score:.1f} < {criteria.min_score:.1f}"
        return entry

    entry.decision = "SHORTLISTED"
    entry.reason = "target=" + ", ".join(targets) if targets else "target=fit_only"
    return entry


def shortlist(jobs_with_scores, profile: CareerProfile, criteria: ShortlistCriteria | None = None) -> ShortlistResult:
    """Rank candidate jobs, enforcing per-company diversity."""
    rules = criteria or ShortlistCriteria()
    result = ShortlistResult()
    seen_companies: dict[str, int] = {}
    for job, score, required, matched, blockers in sorted(jobs_with_scores, key=lambda item: item[1], reverse=True):
        entry = evaluate_job(job, score, required, matched, blockers, rules)
        if entry.accepted:
            company = (job.company or "").casefold()
            if seen_companies.get(company, 0) >= rules.max_per_company:
                entry.decision = "REJECTED"
                entry.reason = f"COMPANY_LIMIT: já selecionada {seen_companies[company]}x"
                result.rejected.append(entry)
                continue
            seen_companies[company] = seen_companies.get(company, 0) + 1
            result.accepted.append(entry)
        else:
            result.rejected.append(entry)
    return result


def summary(result: ShortlistResult) -> dict[str, object]:
    reasons: dict[str, int] = {}
    for entry in result.rejected:
        key = entry.reason.split(":", 1)[0] or "OTHER"
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "considered": result.considered,
        "shortlisted": len(result.accepted),
        "rejected": len(result.rejected),
        "rejection_reasons": dict(sorted(reasons.items(), key=lambda item: -item[1])),
    }
