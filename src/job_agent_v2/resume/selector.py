"""Seleção e agrupamento de fatos sem gerar novos claims."""

from __future__ import annotations

import re

from .models import ResumeProfile, ResumeStrategy


def _terms(value: str) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9+#.-]{3,}", value.casefold())}


def select_fact_ids(
    profile: ResumeProfile,
    strategy: ResumeStrategy,
    title: str,
    description: str = "",
    *,
    max_facts: int = 8,
) -> tuple[str, ...]:
    wanted = _terms(" ".join(strategy.focus + strategy.secondary + (title, description)))
    scored: list[tuple[float, int, str]] = []
    order = 0
    for experience in profile.experiences:
        for fact_id in experience.fact_ids:
            fact = profile.facts.get(fact_id)
            if fact is None or not fact.verified:
                continue
            fact_terms = _terms(" ".join(fact.tags) + " " + fact.statement(strategy.language))
            score = float(len(wanted & fact_terms) * 3)
            score += float(len(_terms(" ".join(strategy.focus)) & fact_terms) * 2)
            scored.append((score, order, fact_id))
            order += 1
    ranked = [fact_id for _, _, fact_id in sorted(scored, key=lambda item: (-item[0], item[1]))]
    if not ranked:
        ranked = [fact_id for experience in profile.experiences for fact_id in experience.fact_ids if fact_id in profile.facts]
    return tuple(dict.fromkeys(ranked))[:max_facts]


def cluster_fact_ids(fact_ids: tuple[str, ...], profile: ResumeProfile, max_per_claim: int = 2) -> tuple[tuple[str, ...], ...]:
    clusters: list[list[str]] = []
    for fact_id in fact_ids:
        fact = profile.facts[fact_id]
        tags = {tag.casefold() for tag in fact.tags}
        target = next((cluster for cluster in clusters if len(cluster) < max_per_claim and tags & {tag.casefold() for item in cluster for tag in profile.facts[item].tags}), None)
        if target is None:
            clusters.append([fact_id])
        else:
            target.append(fact_id)
    return tuple(tuple(cluster) for cluster in clusters)
