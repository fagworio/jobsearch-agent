"""Configuração validada, sem segredos impressos."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    db_path: Path
    artifacts_dir: Path
    profile_path: Path
    facts_path: Path
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    request_timeout: float = 20.0
    real_profile: bool = False

    @classmethod
    def from_args(cls, root: Path | None = None, **values: object) -> "Settings":
        project_root = (root or Path.cwd()).resolve()
        def path_arg(name: str, default: str) -> Path:
            raw = values.get(name) or os.getenv(name.upper()) or default
            return Path(str(raw)).expanduser()
        return cls(
            root=project_root,
            db_path=path_arg("db", "data/jobsearch.db"),
            artifacts_dir=path_arg("artifacts", "data/applications"),
            profile_path=path_arg("profile", "profile/career_profile.yaml"),
            facts_path=path_arg("facts", "profile/locked_facts.yaml"),
            llm_base_url=os.getenv("JOBSEARCH_LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=os.getenv("JOBSEARCH_LLM_API_KEY", ""),
            llm_model=os.getenv("JOBSEARCH_LLM_MODEL", ""),
            request_timeout=float(os.getenv("JOBSEARCH_HTTP_TIMEOUT", "20")),
            real_profile=bool(values.get("real_profile", False)),
        )

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.root / path

