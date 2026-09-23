"""Descoberta somente leitura sobre APIs públicas de job boards de ATS.

Diferente de scraping de agregadores, estas APIs são endpoints JSON públicos e
documentados, publicados pelas próprias plataformas de ATS para alimentar os
job boards delas. Nenhuma autenticação, sessão ou credencial é usada, e o
módulo não escreve nada: apenas faz GET e normaliza para o domínio ``Job``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Iterable


try:  # pragma: no cover - dependência principal, fallback do ambiente mínimo
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from .models import Job
from .sources import SourceError, _job, _json_safe, _text


class BoardError(SourceError):
    """Falha ao consultar ou interpretar o board público de um ATS."""


@dataclass(frozen=True)
class BoardSpec:
    provider: str
    origin: str
    path: str
    allowed_hosts: tuple[str, ...]


_SPECS: dict[str, BoardSpec] = {
    "greenhouse": BoardSpec(
        "greenhouse",
        "https://boards-api.greenhouse.io",
        "/v1/boards/{board}/jobs?content=true",
        ("boards-api.greenhouse.io",),
    ),
    "lever": BoardSpec(
        "lever",
        "https://api.lever.co",
        "/v0/postings/{board}?mode=json",
        ("api.lever.co",),
    ),
    "ashby": BoardSpec(
        "ashby",
        "https://api.ashbyhq.com",
        "/posting-api/job-board/{board}",
        ("api.ashbyhq.com",),
    ),
}


def supported_providers() -> list[str]:
    return sorted(_SPECS)


def board_spec(provider: str) -> BoardSpec:
    spec = _SPECS.get(provider.casefold().strip())
    if spec is None:
        raise BoardError(f"unsupported board provider: {provider}")
    return spec


def _board_url(provider: str, board: str) -> str:
    spec = board_spec(provider)
    token = board.strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", token):
        raise BoardError(f"invalid board token for {spec.provider}: {board!r}")
    return spec.origin + spec.path.format(board=token)


def _text_from_html(value: Any) -> str:
    """HTML -> texto, desescapando entidades antes de remover tags.

    O campo ``content`` do Greenhouse vem com o HTML escapado (``&lt;p&gt;``).
    Sem desescapar, a descricao guardava markup literal e os titulos de secao
    ficavam separados do conteudo: um "Nice-to-have skills" virava frase
    propria e as skills abaixo eram classificadas como obrigatorias.
    """
    raw = _text(value)
    if not raw:
        return ""
    if "<" not in raw and "&lt;" not in raw and "&#" not in raw:
        return raw
    unescaped = html.unescape(raw)
    try:  # pragma: no cover - exercised when bs4 is installed
        from bs4 import BeautifulSoup

        return re.sub(r"\s+", " ", BeautifulSoup(unescaped, "html.parser").get_text(" ", strip=True)).strip()
    except ImportError:  # pragma: no cover
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescaped)).strip()


def _greenhouse(record: dict[str, Any], board: str) -> tuple[dict[str, Any], str]:
    location = record.get("location")
    if isinstance(location, dict):
        location = location.get("name") or location.get("location_str") or ""
    canonical = dict(record)
    canonical.update(
        {
            "company": board,
            "title": record.get("title"),
            "location": location,
            "description": _text_from_html(record.get("content")),
            "posted_at": record.get("first_published") or record.get("updated_at"),
        }
    )
    return canonical, _text(record.get("absolute_url"))


def _lever(record: dict[str, Any], board: str) -> tuple[dict[str, Any], str]:
    categories = record.get("categories") if isinstance(record.get("categories"), dict) else {}
    canonical = dict(record)
    canonical.update(
        {
            "company": board,
            "title": record.get("text"),
            "location": categories.get("location"),
            "employment_type": categories.get("commitment") or "unknown",
            "description": _text_from_html(record.get("descriptionPlain") or record.get("description")),
            "posted_at": record.get("createdAt"),
        }
    )
    return canonical, _text(record.get("hostedUrl") or record.get("applyUrl"))


def _ashby(record: dict[str, Any], board: str) -> tuple[dict[str, Any], str]:
    canonical = dict(record)
    canonical.update(
        {
            "company": board,
            "title": record.get("title"),
            "location": record.get("location"),
            "employment_type": record.get("employmentType") or "unknown",
            "description": _text_from_html(record.get("descriptionHtml") or record.get("descriptionPlain")),
            "posted_at": record.get("publishedAt"),
        }
    )
    return canonical, _text(record.get("jobUrl") or record.get("applyUrl"))


_NORMALIZERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def _records(provider: str, payload: Any) -> list[dict[str, Any]]:
    if provider == "lever":
        if not isinstance(payload, list):
            raise BoardError("lever board response must be a list of postings")
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise BoardError(f"{provider} board response must be a JSON object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise BoardError(f"{provider} board response has no jobs list")
    return [item for item in jobs if isinstance(item, dict)]


def _matches(job: Job, *, query: str, location: str) -> bool:
    haystack = f"{job.title} {job.description}".casefold()
    if query and not all(token in haystack for token in query.casefold().split()):
        return False
    if location and location.casefold() not in f"{job.location} {job.country}".casefold():
        return False
    return True


def discover_board(
    provider: str,
    board: str,
    *,
    timeout: float = 20.0,
    client: Any | None = None,
    query: str = "",
    location: str = "",
    limit: int = 0,
) -> list[Job]:
    """Fetch a public ATS board and normalize every posting into a ``Job``.

    The board URL is always built from the provider's own approved origin; a
    caller-supplied ``client`` only changes transport, which is how tests route
    the real request shape to a local fixture.
    """
    spec = board_spec(provider)
    url = _board_url(spec.provider, board)
    if httpx is None:  # pragma: no cover - httpx é dependência principal
        raise BoardError("httpx is required for board discovery")
    owned = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        response = http.get(url, timeout=timeout)
    except Exception as exc:  # rede/transporte
        raise BoardError(f"{spec.provider} board request failed: {exc}") from exc
    finally:
        if owned:
            http.close()
    if response.status_code != 200:
        raise BoardError(f"{spec.provider} board returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise BoardError(f"{spec.provider} board did not return JSON") from exc

    normalizer = _NORMALIZERS[spec.provider]
    jobs: list[Job] = []
    for record in _records(spec.provider, payload):
        canonical, posting_url = normalizer(record, board.strip())
        canonical["source"] = spec.provider
        job = _job(spec.provider, _json_safe(canonical), posting_url, company=board.strip())
        if _matches(job, query=query, location=location):
            jobs.append(job)
    if limit > 0:
        jobs = jobs[:limit]
    return jobs


def discover_many(
    boards: Iterable[tuple[str, str]],
    *,
    timeout: float = 20.0,
    client: Any | None = None,
    query: str = "",
    location: str = "",
    limit: int = 0,
) -> tuple[list[Job], list[dict[str, str]]]:
    """Discover several boards, reporting per-board failures instead of aborting."""
    jobs: list[Job] = []
    failures: list[dict[str, str]] = []
    for provider, board in boards:
        try:
            jobs.extend(
                discover_board(
                    provider,
                    board,
                    timeout=timeout,
                    client=client,
                    query=query,
                    location=location,
                    limit=limit,
                )
            )
        except BoardError as exc:
            failures.append({"provider": provider, "board": board, "error": str(exc)})
    if limit > 0:
        jobs = jobs[:limit]
    return jobs, failures
