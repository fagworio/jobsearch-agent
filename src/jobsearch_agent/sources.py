"""Ingestão e normalização de vagas, API/JSON primeiro e HTML depois."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import Job, JobState, now_iso


class SourceError(RuntimeError):
    pass


class SourceAdapter(Protocol):
    name: str
    def can_handle(self, url: str, payload: dict[str, Any] | None = None) -> bool: ...
    def normalize(self, payload: dict[str, Any], url: str = "") -> Job: ...


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value).strip()


def _external_id(url: str, payload: dict[str, Any]) -> str:
    for key in ("id", "job_id", "requisition_id", "external_id"):
        if payload.get(key):
            return _text(payload[key])
    match = re.search(r"(?:jobs|positions|postings)[/.-]([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else hashlib.sha1(url.encode()).hexdigest()[:16]


def _description(payload: dict[str, Any]) -> str:
    for key in ("description", "content", "description_html", "body", "text"):
        if payload.get(key):
            return _text(payload[key])
    return ""


def _job(source: str, payload: dict[str, Any], url: str, *, company: str = "") -> Job:
    company_value = company or _text(payload.get("company") or payload.get("company_name"))
    title = _text(payload.get("title") or payload.get("name") or payload.get("text"))
    location = _text(payload.get("location"))
    if isinstance(payload.get("location"), dict):
        location = _text(payload["location"].get("name") or payload["location"].get("location_str"))
    return Job(
        id=hashlib.sha1(f"{source}:{_external_id(url, payload)}".encode()).hexdigest()[:16],
        source=source,
        external_id=_external_id(url, payload),
        company=company_value,
        title=title,
        description=_description(payload),
        location=location,
        country=_text(payload.get("country")),
        remote_type=_text(payload.get("remote_type") or payload.get("workplace_type") or "unknown"),
        employment_type=_text(payload.get("employment_type") or payload.get("employmentType") or "unknown"),
        salary=_text(payload.get("salary") or payload.get("compensation")),
        currency=_text(payload.get("currency")),
        url=url,
        posted_at=_text(payload.get("posted_at") or payload.get("published_at")),
        discovered_at=now_iso(),
        raw_payload=payload,
        state=JobState.NORMALIZED,
    )


class GreenhouseAdapter:
    name = "greenhouse"
    def can_handle(self, url: str, payload: dict[str, Any] | None = None) -> bool:
        return "greenhouse.io" in url or bool(payload and ("absolute_url" in payload or "greenhouse" in payload.get("source", "")))
    def normalize(self, payload: dict[str, Any], url: str = "") -> Job:
        return _job(self.name, payload, url or _text(payload.get("absolute_url")), company=_text(payload.get("company_name")))


class LeverAdapter:
    name = "lever"
    def can_handle(self, url: str, payload: dict[str, Any] | None = None) -> bool:
        return "jobs.lever.co" in url or bool(payload and ("lever" in payload.get("source", "") or "categories" in payload))
    def normalize(self, payload: dict[str, Any], url: str = "") -> Job:
        categories = payload.get("categories") if isinstance(payload.get("categories"), dict) else {}
        merged = dict(payload)
        merged.setdefault("location", categories.get("location", ""))
        merged.setdefault("employment_type", categories.get("commitment", ""))
        return _job(self.name, merged, url or _text(payload.get("hostedUrl")), company=_text(payload.get("company")))


class AshbyAdapter:
    name = "ashby"
    def can_handle(self, url: str, payload: dict[str, Any] | None = None) -> bool:
        return "ashbyhq.com" in url or bool(payload and "ashby" in payload.get("source", ""))
    def normalize(self, payload: dict[str, Any], url: str = "") -> Job:
        return _job(self.name, payload, url or _text(payload.get("jobUrl") or payload.get("applicationUrl")), company=_text(payload.get("organizationName")))


class JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_script = False
        self.buffer: list[str] = []
        self.payloads: list[dict[str, Any]] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and any(k == "type" and v == "application/ld+json" for k, v in attrs):
            self.in_script = True
            self.buffer = []
    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.in_script:
            self.in_script = False
            try:
                data = json.loads("".join(self.buffer))
                items = data if isinstance(data, list) else [data]
                self.payloads.extend(item for item in items if isinstance(item, dict))
            except json.JSONDecodeError:
                pass
    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.buffer.append(data)


class GenericAdapter:
    name = "generic"
    def can_handle(self, url: str, payload: dict[str, Any] | None = None) -> bool:
        return True
    def normalize(self, payload: dict[str, Any], url: str = "") -> Job:
        normalized = dict(payload)
        organization = payload.get("hiringOrganization")
        if isinstance(organization, dict):
            normalized.setdefault("company", organization.get("name", ""))
        return _job(self.name, normalized, url or _text(payload.get("url")))


ADAPTERS: tuple[SourceAdapter, ...] = (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), GenericAdapter())


def choose_adapter(url: str, payload: dict[str, Any] | None = None) -> SourceAdapter:
    return next(adapter for adapter in ADAPTERS if adapter.can_handle(url, payload))


def fetch_payload(url: str, timeout: float = 20.0) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "jobsearch-agent/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("content-type", "")
    except Exception as exc:
        raise SourceError(f"could not fetch job URL: {exc}") from exc
    try:
        if "json" in content_type or body.lstrip().startswith((b"{", b"[")):
            value = json.loads(body.decode("utf-8"))
            return value if isinstance(value, dict) else {"description": json.dumps(value)}
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    parser = JsonLdParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    job_posting = next((item for item in parser.payloads if item.get("@type") in ("JobPosting", ["JobPosting"])), None)
    if job_posting:
        return job_posting
    title = re.search(r"<title[^>]*>(.*?)</title>", body.decode("utf-8", errors="replace"), re.I | re.S)
    return {"title": re.sub(r"<[^>]+>", " ", title.group(1)).strip() if title else "", "description": body.decode("utf-8", errors="replace")}


def normalize_payload(payload: dict[str, Any], url: str = "") -> Job:
    adapter = choose_adapter(url, payload)
    return adapter.normalize(payload, url)


def canonical_job_key(job: Job) -> str:
    if job.source and job.external_id:
        return f"source:{job.source}:{job.external_id}"
    if job.url:
        return f"url:{job.url.rstrip('/').lower()}"
    normalized = re.sub(r"\W+", " ", f"{job.company} {job.title} {job.location}".lower()).strip()
    return f"text:{normalized}"
