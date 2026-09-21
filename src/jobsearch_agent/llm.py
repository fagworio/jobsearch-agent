"""Porta opcional de LLM; o domínio permanece executável offline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.request import Request, urlopen


class LLMError(RuntimeError):
    pass


@dataclass
class LLMRequest:
    system: str
    user: str
    schema: str


class LLMProvider:
    def complete(self, request: LLMRequest) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> dict:
        if not self.base_url or not self.api_key or not self.model:
            raise LLMError("LLM provider is not configured")
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            "response_format": {"type": "json_object"},
        }
        http_request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            value = json.loads(content) if isinstance(content, str) else content
            if not isinstance(value, dict):
                raise ValueError("response is not an object")
            return value
        except Exception as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc


class FakeProvider(LLMProvider):
    """Provider usado por fixtures e testes; devolve apenas dados grounded."""

    def complete(self, request: LLMRequest) -> dict:
        return {}

