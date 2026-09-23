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
        self._fuzzy_index = self._build_fuzzy_index()

    def _build_fuzzy_index(self) -> dict[tuple[int, str], list[tuple[str, "Skill"]]]:
        """Bucket single-word aliases by (normalized length, first character).

        The fuzzy fallback only exists for single-token spelling variants;
        multi-word variants are listed explicitly and caught by the literal
        pass. Bucketing by length and first letter turns the scan from
        text × skills × aliases of WRatio calls into a handful of dict lookups
        per token, most of which miss.
        """
        index: dict[tuple[int, str], list[tuple[str, Skill]]] = {}
        for skill in self.skills:
            for alias in (skill.label, *skill.aliases):
                if " " in alias.strip():
                    continue
                alias_norm = normalize(alias)
                if len(alias_norm) <= 3:
                    continue
                index.setdefault((len(alias_norm), alias_norm[0]), []).append((alias_norm, skill))
        return index

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
        if len(candidate) <= 3:
            return None
        scored = max(((self._score(candidate, alias), skill) for skill in self.skills for alias in (skill.label, *skill.aliases)), key=lambda item: item[0], default=(0, None))
        return scored[1] if scored[0] >= threshold else None

    def matches(self, left: str, right: str, threshold: float = 88.0) -> bool:
        left_skill = self.resolve(left, threshold)
        right_skill = self.resolve(right, threshold)
        if left_skill and right_skill:
            return left_skill.key == right_skill.key
        left_norm = normalize(left)
        right_norm = normalize(right)
        # Formas muito curtas nao podem usar fuzzy: `normalize("C#")` e "c", e
        # a similaridade parcial casa "c" com "css", "custom-plugins" e
        # qualquer outra coisa. Isso marcava C# como presente no perfil de um
        # desenvolvedor frontend e inflava o fit de vagas .NET.
        if len(left_norm) <= 2 or len(right_norm) <= 2:
            return left_norm == right_norm
        return self._score(left_norm, right_norm) >= threshold

    def extract(self, text: str, threshold: float = 88.0) -> list[str]:
        lowered = text.lower()
        found: list[Skill] = []
        pending: list[Skill] = []
        for skill in self.skills:
            aliases = (skill.label, *skill.aliases)
            if any(re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", lowered) for alias in aliases):
                found.append(skill)
            else:
                pending.append(skill)
        if pending:
            found.extend(self._fuzzy_pass(lowered, pending, threshold))
        return [skill.label for skill in self.skills if skill in found]

    def _fuzzy_pass(self, lowered: str, pending: list["Skill"], threshold: float) -> list["Skill"]:
        """Catch single-token spelling variants without accepting partial hits.

        ``WRatio`` scores a single token against a longer multi-word alias very
        highly (``"github"`` vs ``"github actions"`` scores 90), which would map
        an unrelated word onto a skill. Only single tokens are compared, only
        against single-word aliases, and only when the normalized lengths are
        comparable; multi-word variants belong in the explicit ``aliases`` list,
        where the literal pass picks them up.
        """
        pending_set = set(pending)
        matched: set[Skill] = set()
        for token in re.findall(r"[a-z0-9.+#-]+", lowered):
            token_norm = normalize(token)
            length = len(token_norm)
            if length <= 3:
                continue
            slack = max(2, length // 3)
            for candidate_length in range(max(4, length - slack - 1), length + slack + 2):
                for alias_norm, skill in self._fuzzy_index.get((candidate_length, token_norm[0]), ()):  # noqa: E501
                    if skill in matched or skill not in pending_set:
                        continue
                    if abs(length - candidate_length) > max(2, candidate_length // 3):
                        continue
                    if self._score(token_norm, alias_norm) >= threshold:
                        matched.add(skill)
                        if len(matched) == len(pending_set):
                            return [skill for skill in pending if skill in matched]
        return [skill for skill in pending if skill in matched]

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
