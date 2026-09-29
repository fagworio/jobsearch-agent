"""Matriz declarativa de consultas para a primeira fonte de discovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


DEFAULT_SEARCH_FAMILIES: Mapping[str, tuple[str, ...]] = {
    "primary": (
        "wordpress developer",
        "wordpress engineer",
        "senior wordpress developer",
        "wordpress",
        "woocommerce developer",
    ),
    "frontend": (
        "frontend developer",
        "front end developer",
        "frontend engineer",
        "front end engineer",
        "web developer",
        "senior web developer",
    ),
    "php": (
        "php developer",
        "php engineer",
        "wordpress php",
        "cms developer",
    ),
    "ecommerce": (
        "shopify developer",
        "ecommerce developer",
        "woocommerce",
        "shopify",
    ),
    "broader": (
        "full stack developer",
        "web applications developer",
        "web engineer",
    ),
}


@dataclass(frozen=True)
class SearchQuery:
    family: str
    query: str


@dataclass(frozen=True)
class SearchBudget:
    target_ready_jobs: int = 3
    max_queries: int = 20
    max_jobs_inspected: int = 250
    max_pages: int = 40

    def validate(self) -> None:
        if self.target_ready_jobs < 0 or self.max_queries <= 0 or self.max_jobs_inspected <= 0 or self.max_pages <= 0:
            raise ValueError("search budgets must be positive, except target_ready_jobs")


@dataclass(frozen=True)
class SearchCursor:
    queries_processed: int = 0
    jobs_inspected: int = 0
    pages: int = 0
    ready_jobs: int = 0

    def should_continue(self, budget: SearchBudget) -> bool:
        budget.validate()
        return (
            self.ready_jobs < budget.target_ready_jobs
            and self.queries_processed < budget.max_queries
            and self.jobs_inspected < budget.max_jobs_inspected
            and self.pages < budget.max_pages
        )


def build_query_matrix() -> tuple[SearchQuery, ...]:
    """Expande a matriz na ordem estável do perfil, sem duplicar consultas."""

    matrix: list[SearchQuery] = []
    seen: set[str] = set()
    for family, queries in DEFAULT_SEARCH_FAMILIES.items():
        for query in queries:
            normalized = query.casefold().strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            matrix.append(SearchQuery(family=family, query=query))
    return tuple(matrix)


__all__ = ["DEFAULT_SEARCH_FAMILIES", "SearchBudget", "SearchCursor", "SearchQuery", "build_query_matrix"]
