"""Configuração validada, sem segredos impressos."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover - fallback do ambiente mínimo
    BaseSettings = None
    SettingsConfigDict = None


if BaseSettings:
    class EnvironmentSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="JOBSEARCH_", extra="ignore")
        llm_base_url: str = ""
        llm_api_key: str = ""
        llm_model: str = ""
        http_timeout: float = 20.0
else:
    class EnvironmentSettings:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.llm_base_url = os.getenv("JOBSEARCH_LLM_BASE_URL", "")
            self.llm_api_key = os.getenv("JOBSEARCH_LLM_API_KEY", "")
            self.llm_model = os.getenv("JOBSEARCH_LLM_MODEL", "")
            self.http_timeout = float(os.getenv("JOBSEARCH_HTTP_TIMEOUT", "20"))


@dataclass(frozen=True)
class Settings:
    root: Path
    db_path: Path
    artifacts_dir: Path
    profile_path: Path
    facts_path: Path
    preferences_path: Path
    answers_path: Path
    application_policy_path: Path
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    request_timeout: float = 20.0
    real_profile: bool = False

    @classmethod
    def from_args(cls, root: Path | None = None, **values: object) -> "Settings":
        project_root = (root or Path.cwd()).resolve()
        environment = EnvironmentSettings()
        local_profile = project_root / "profile/career_profile.local.yaml"
        local_facts = project_root / "profile/locked_facts.local.yaml"
        local_preferences = project_root / "profile/preferences.local.yaml"
        def path_arg(name: str, default: str) -> Path:
            raw = values.get(name) or os.getenv(name.upper()) or default
            return Path(str(raw)).expanduser()
        return cls(
            root=project_root,
            db_path=path_arg("db", "data/jobsearch.db"),
            artifacts_dir=path_arg("artifacts", "data/applications"),
            profile_path=path_arg("profile", str(local_profile if local_profile.is_file() else Path("profile/career_profile.yaml"))),
            facts_path=path_arg("facts", str(local_facts if local_facts.is_file() else Path("profile/locked_facts.yaml"))),
            preferences_path=path_arg("preferences", str(local_preferences if local_preferences.is_file() else Path("profile/preferences.yaml"))),
            answers_path=path_arg("answers", "profile/answers.yaml"),
            application_policy_path=path_arg("application_policy", "profile/application_policy.yaml"),
            llm_base_url=environment.llm_base_url.rstrip("/"),
            llm_api_key=environment.llm_api_key,
            llm_model=environment.llm_model,
            request_timeout=environment.http_timeout,
            real_profile=bool(values.get("real_profile", False)),
        )

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.root / path
