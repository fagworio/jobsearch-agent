"""Configuração pequena e explícita do produto V2."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


@dataclass(frozen=True)
class V2Config:
    native_host_name: str = "com.job_agent_v2"
    protocol_version: int = 1
    timeout_ms: float = 45_000

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "V2Config":
        values = environ or os.environ
        raw_timeout = values.get("JOB_AGENT_V2_TIMEOUT_MS", "45000")
        try:
            timeout_ms = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("JOB_AGENT_V2_TIMEOUT_MS must be numeric") from exc
        if timeout_ms <= 0:
            raise ValueError("JOB_AGENT_V2_TIMEOUT_MS must be positive")
        return cls(timeout_ms=timeout_ms)
