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
