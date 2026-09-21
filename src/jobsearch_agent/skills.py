"""Registry de skills, aliases e matching lexical com fallback sem dependências."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

try:
    from rapidfuzz.fuzz import WRatio as _fuzzy_ratio
except ImportError:  # pragma: no cover - exercitado quando a dependência opcional não está instalada
    def _fuzzy_ratio(left: str, right: str) -> float:
        return difflib.SequenceMatcher(None, left, right).ratio() * 100


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@dataclass(frozen=True)
class Skill:
    key: str
    label: str
    aliases: tuple[str, ...]
    categories: tuple[str, ...]


class SkillRegistry:
    def __init__(self, skills: Iterable[Skill]):
        self.skills = tuple(skills)
        self._by_key = {skill.key: skill for skill in self.skills}

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SkillRegistry":
        candidates = []
        if path:
            candidates.append(Path(path))
        candidates.extend([
            Path.cwd() / "knowledge/skills.yaml",
            Path(__file__).parent / "knowledge/skills.yaml",
            Path(__file__).parents[2] / "knowledge/skills.yaml",
        ])
        for candidate in candidates:
            if candidate.is_file():
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                return cls(Skill(str(key), str(value.get("label", key)), tuple(str(item) for item in value.get("aliases", [])), tuple(str(item) for item in value.get("categories", []))) for key, value in data.items())
        return cls(_builtin_skills())

    def resolve(self, value: str, threshold: float = 88.0) -> Skill | None:
        candidate = normalize(value)
        for skill in self.skills:
            values = (skill.key, skill.label, *skill.aliases)
            if any(candidate == normalize(item) for item in values):
                return skill
        scored = max(((self._score(candidate, alias), skill) for skill in self.skills for alias in (skill.label, *skill.aliases)), key=lambda item: item[0], default=(0, None))
        return scored[1] if scored[0] >= threshold else None

    def matches(self, left: str, right: str, threshold: float = 88.0) -> bool:
        left_skill = self.resolve(left, threshold)
        right_skill = self.resolve(right, threshold)
        if left_skill and right_skill:
            return left_skill.key == right_skill.key
        return self._score(normalize(left), normalize(right)) >= threshold

    def extract(self, text: str, threshold: float = 88.0) -> list[str]:
        lowered = text.lower()
        found: list[str] = []
        for skill in self.skills:
            aliases = (skill.label, *skill.aliases)
            if any(re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", lowered) for alias in aliases):
                found.append(skill.label)
                continue
            # Tenta janelas curtas para variações como “RESTful APIs”.
            words = re.findall(r"[a-z0-9.+#-]+", lowered)
            for size in range(1, min(4, len(words)) + 1):
                if any(self._score(normalize(" ".join(words[index:index + size])), normalize(alias)) >= threshold for index in range(len(words) - size + 1) for alias in aliases):
                    found.append(skill.label)
                    break
        return found

    @staticmethod
    def _score(left: str, right: str) -> float:
        return float(_fuzzy_ratio(left, right))


def _builtin_skills() -> tuple[Skill, ...]:
    return (
        Skill("wordpress", "WordPress", ("wordpress", "wp"), ("cms",)),
        Skill("woocommerce", "WooCommerce", ("woocommerce", "woo commerce"), ("ecommerce",)),
        Skill("php", "PHP", ("php",), ("backend",)),
        Skill("python", "Python", ("python", "py"), ("backend",)),
        Skill("rest_api", "REST API", ("rest api", "rest apis", "restful api", "api integration"), ("backend",)),
        Skill("docker", "Docker", ("docker",), ("devops",)),
        Skill("git", "Git", ("git",), ("tools",)),
    )
