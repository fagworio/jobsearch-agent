"""Validação factual, ATS e métricas de cobertura do currículo V2."""

from __future__ import annotations

import re
from typing import Any

from .models import ResumeDocument, ResumeProfile, ResumeStrategy, ResumeValidation


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.-]{3,}", value.casefold()))


def validate_facts(document: ResumeDocument, profile: ResumeProfile) -> ResumeValidation:
    errors: list[str] = []
    for claim in document.claims:
        supported = [profile.facts.get(fact_id) for fact_id in claim.supported_by]
        if not claim.supported_by or any(fact is None or not fact.verified for fact in supported):
            errors.append(f"unsupported claim: {claim.claim}")
            continue
        corpus = " ".join(fact.statement(document.language) for fact in supported if fact is not None)
        claim_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", claim.claim))
        corpus_numbers = set(re.findall(r"\d+(?:[.,]\d+)?%?", corpus))
        if claim_numbers - corpus_numbers:
            errors.append(f"claim contains unsupported numbers: {claim.claim}")
            continue
        if not _tokens(claim.claim) <= _tokens(corpus):
            errors.append(f"claim does not match supporting facts: {claim.claim}")
    return ResumeValidation(not errors, "RESUME_VALIDATION_FAILED" if errors else "OK", tuple(errors))


def render_content(document: ResumeDocument) -> str:
    parts = [document.header.get("name", ""), document.header.get("email", ""), document.summary, " ".join(document.skills)]
    for row in document.experience:
        parts.extend([str(row.get("role", "")), str(row.get("company", ""))] + [str(item) for item in row.get("bullets", [])])
    for row in document.education:
        parts.extend([str(row.get("institution", "")), str(row.get("credential", "")), str(row.get("field_of_study", ""))])
    return " ".join(parts)


def validate_ats(document: ResumeDocument, profile: ResumeProfile) -> ResumeValidation:
    errors: list[str] = []
    if not document.header.get("name"):
        errors.append("contact name is missing")
    if not document.header.get("email"):
        errors.append("contact email is missing")
    if not document.summary:
        errors.append("summary is missing")
    if not document.experience or any(not row.get("bullets") for row in document.experience):
        errors.append("experience is missing or has no bullets")
    if profile.education and not document.education:
        errors.append("education is missing")
    if not document.one_column:
        errors.append("document is not single-column")
    return ResumeValidation(not errors, "RESUME_ATS_VALIDATION_FAILED" if errors else "OK", tuple(errors))


def quality_metrics(document: ResumeDocument, strategy: ResumeStrategy, job_text: str) -> dict[str, Any]:
    content = _tokens(render_content(document))
    targets = [keyword for keyword in strategy.focus + strategy.secondary if keyword]
    covered = [keyword for keyword in targets if _tokens(keyword) & content]
    return {
        "required_skills_covered": len(covered),
        "required_skills_total": len(targets),
        "preferred_skills_covered": len([keyword for keyword in strategy.secondary if _tokens(keyword) & content]),
        "preferred_skills_total": len(strategy.secondary),
        "unsupported_claims": 0,
        "target_keywords_present": covered,
        "job_text_observed": bool(job_text.strip()),
    }


def validate_document(document: ResumeDocument, profile: ResumeProfile, strategy: ResumeStrategy, job_text: str) -> ResumeValidation:
    factual = validate_facts(document, profile)
    ats = validate_ats(document, profile)
    metrics = quality_metrics(document, strategy, job_text)
    errors = tuple(factual.errors + ats.errors)
    return ResumeValidation(not errors, "OK" if not errors else "RESUME_VALIDATION_FAILED", errors, metrics=metrics)
