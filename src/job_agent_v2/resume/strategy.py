"""Estratégia determinística de posicionamento por vaga."""

from __future__ import annotations

import re
import unicodedata

from .models import ResumeProfile, ResumeStrategy


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _contains(text: str, alias: str) -> bool:
    normalized = _normalise(alias).strip()
    return bool(normalized) and re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text)


def detect_language(text: str, override: str | None = None) -> str:
    if override:
        return "pt-BR" if override.casefold().startswith("pt") else "en-US"
    normalized = _normalise(text)
    portuguese = sum(1 for word in (" experiência ", " desenvolvimento ", " vaga ", " responsabilidades ", " conhecimento ") if word in f" {normalized} ")
    return "pt-BR" if portuguese >= 2 else "en-US"


def build_strategy(
    profile: ResumeProfile,
    title: str,
    description: str = "",
    *,
    language_override: str | None = None,
) -> ResumeStrategy:
    text = _normalise(f"{title} {description}")
    role = title.strip() or "Web Developer"
    matched: list[str] = []
    remaining: list[str] = []
    for key, aliases in profile.skills:
        label = key.replace("_", " ").strip()
        if any(_contains(text, alias) for alias in aliases):
            matched.append(label)
        else:
            remaining.append(label)

    role_cues = [
        ("frontend", ("frontend", "front end", "front-end", "javascript", "react", "vue", "angular")),
        ("ecommerce", ("ecommerce", "e-commerce", "woocommerce", "shopify", "magento")),
        ("wordpress", ("wordpress", "gutenberg", "custom plugins", "custom themes")),
        ("platform integration", ("api", "integration", "headless", "middleware")),
    ]
    positioning = next((label for label, cues in role_cues if any(_contains(text, cue) for cue in cues)), "web development")
    focus = list(dict.fromkeys(matched))
    if not focus:
        focus = [positioning]
    secondary = [item for item in remaining if item not in focus][:6]
    deprioritize = tuple(item for item in remaining if item not in secondary)
    keywords = tuple(dict.fromkeys(focus + secondary))[:12]
    return ResumeStrategy(
        target_role=role,
        language=detect_language(f"{title} {description}", language_override),
        positioning=positioning,
        focus=tuple(focus),
        secondary=tuple(secondary),
        deprioritize=deprioritize,
        keywords=keywords,
        max_pages=profile.max_pages,
        template=profile.template,
    )
