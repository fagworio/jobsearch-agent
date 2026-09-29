"""Geração factual e determinística do documento por vaga."""

from __future__ import annotations

from .models import ResumeClaim, ResumeDocument, ResumeProfile, ResumeStrategy
from .selector import cluster_fact_ids


def _localized(value: dict[str, str], language: str) -> str:
    return value.get(language) or value.get("en-US") or next(iter(value.values()), "")


def _dynamic_summary(profile: ResumeProfile, strategy: ResumeStrategy, selected: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    base_ids = tuple(fact_id for fact_id in profile.summary_fact_ids.get(strategy.language, ()) if fact_id in profile.facts and profile.facts[fact_id].verified)
    base = profile.summaries.get(strategy.language) or profile.summaries.get("en-US", "")
    relevant = tuple(fact_id for fact_id in selected if fact_id in profile.facts and strategy.positioning in {tag.casefold() for tag in profile.facts[fact_id].tags})
    extra = profile.facts[relevant[0]].statement(strategy.language) if relevant else ""
    if extra and extra not in base:
        return f"{base} {extra}", tuple(dict.fromkeys(base_ids + relevant[:1]))
    return base, base_ids


def generate_document(
    job_id: str,
    title: str,
    company: str,
    profile: ResumeProfile,
    strategy: ResumeStrategy,
    selected_ids: tuple[str, ...],
) -> ResumeDocument:
    claims: list[ResumeClaim] = []
    summary, summary_ids = _dynamic_summary(profile, strategy, selected_ids)
    if summary:
        claims.append(ResumeClaim(summary, summary_ids, bool(summary_ids)))

    experience_rows: list[dict[str, object]] = []
    selected_set = set(selected_ids)
    for experience in profile.experiences:
        fact_ids = tuple(fact_id for fact_id in experience.fact_ids if fact_id in selected_set and fact_id in profile.facts and profile.facts[fact_id].verified)
        if not fact_ids:
            continue
        bullets: list[str] = []
        for cluster in cluster_fact_ids(fact_ids, profile):
            bullet = " ".join(profile.facts[fact_id].statement(strategy.language).strip() for fact_id in cluster).strip()
            bullets.append(bullet)
            claims.append(ResumeClaim(bullet, cluster, True))
        experience_rows.append({
            "company": experience.company,
            "role": experience.role,
            "start_date": experience.start_date,
            "end_date": experience.end_date,
            "bullets": bullets,
            "fact_ids": list(fact_ids),
        })

    education_rows: list[dict[str, object]] = []
    for education in profile.education:
        fact_ids = tuple(fact_id for fact_id in education.fact_ids if fact_id in profile.facts and profile.facts[fact_id].verified)
        if not fact_ids:
            continue
        education_rows.append({
            "institution": education.institution,
            "credential": _localized(education.credential, strategy.language),
            "field_of_study": _localized(education.field_of_study, strategy.language),
            "start_date": education.start_date,
            "end_date": education.end_date,
            "fact_ids": list(fact_ids),
        })
        for fact_id in fact_ids:
            claims.append(ResumeClaim(profile.facts[fact_id].statement(strategy.language), (fact_id,), True))

    return ResumeDocument(
        resume_id=f"resume-{job_id}-{strategy.language}",
        job_id=job_id,
        company=company,
        target_role=title,
        language=strategy.language,
        header={key: profile.identity.get(key, "") for key in ("name", "email", "phone", "location", "linkedin", "github", "website")},
        summary=summary,
        skills=strategy.keywords,
        experience=tuple(experience_rows),
        education=tuple(education_rows),
        claims=tuple(claims),
        template=strategy.template,
        one_column=True,
    )
