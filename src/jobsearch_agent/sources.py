"""Ingestão e normalização de vagas, API/JSON primeiro e HTML depois."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import httpx
except ImportError:  # pragma: no cover - fallback do ambiente mínimo
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - fallback do ambiente mínimo
    BeautifulSoup = None

from .models import Job, JobState, now_iso
from .schemas import validate_external_job


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobIdentityCandidate:
    identity_type: str
    identity_value: str
    strength: str
    source: str


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
    for key in ("id", "job_id", "requisition_id", "external_id", "job_url"):
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
        location = payload.get("jobLocation")
        if isinstance(location, list) and location:
            location = location[0]
        if isinstance(location, dict):
            address = location.get("address", {})
            normalized.setdefault("location", address.get("addressLocality") or address.get("addressRegion") or "")
        normalized.setdefault("employment_type", payload.get("employmentType", ""))
        return _job(self.name, normalized, url or _text(payload.get("url")))


class JobSpyAdapter:
    """Adapter opcional: JobSpy descobre, o domínio normaliza e deduplica."""

    name = "jobspy"

    def can_handle(self, url: str, payload: dict[str, Any] | None = None) -> bool:
        return bool(payload and str(payload.get("source", "")).startswith("jobspy"))

    def normalize(self, payload: dict[str, Any], url: str = "") -> Job:
        return _job(self.name, payload, url or _text(payload.get("job_url") or payload.get("job_url_direct")))

    def search(self, query: str, *, sites: list[str] | None = None, location: str = "", results_wanted: int = 20, **kwargs: Any) -> list[Job]:
        try:
            from jobspy import scrape_jobs
        except ImportError as exc:
            raise SourceError("JobSpy is optional; install the discovery dependency first") from exc
        frame = scrape_jobs(
            site_name=sites or ["indeed", "google"],
            search_term=query,
            location=location,
            results_wanted=results_wanted,
            **kwargs,
        )
        jobs: list[Job] = []
        for row in frame.to_dict(orient="records"):
            row["source"] = "jobspy"
            jobs.append(self.normalize(row, _text(row.get("job_url"))))
        return jobs


JOBSPY = JobSpyAdapter()
ADAPTERS: tuple[SourceAdapter, ...] = (GreenhouseAdapter(), LeverAdapter(), AshbyAdapter(), JOBSPY, GenericAdapter())


def choose_adapter(url: str, payload: dict[str, Any] | None = None) -> SourceAdapter:
    return next(adapter for adapter in ADAPTERS if adapter.can_handle(url, payload))


def fetch_payload(url: str, timeout: float = 20.0) -> dict[str, Any]:
    try:
        if httpx:
            response = httpx.get(url, headers={"User-Agent": "jobsearch-agent/0.1"}, follow_redirects=True, timeout=timeout)
            response.raise_for_status()
            body = response.content
            content_type = response.headers.get("content-type", "")
        else:
            request = Request(url, headers={"User-Agent": "jobsearch-agent/0.1"})
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
    html_body = body.decode("utf-8", errors="replace")
    payloads: list[dict[str, Any]] = []
    if BeautifulSoup:
        soup = BeautifulSoup(html_body, "html.parser")
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or script.get_text())
                payloads.extend(data if isinstance(data, list) else [data])
            except (json.JSONDecodeError, TypeError):
                continue
        job_posting = next((item for item in payloads if isinstance(item, dict) and (item.get("@type") == "JobPosting" or isinstance(item.get("@type"), list) and "JobPosting" in item["@type"])), None)
    else:
        parser = JsonLdParser()
        parser.feed(html_body)
        job_posting = next((item for item in parser.payloads if item.get("@type") in ("JobPosting", ["JobPosting"])), None)
    if job_posting:
        return job_posting
    title = soup.title.get_text(" ", strip=True) if BeautifulSoup and soup.title else re.search(r"<title[^>]*>(.*?)</title>", html_body, re.I | re.S)
    title_text = title if isinstance(title, str) else re.sub(r"<[^>]+>", " ", title.group(1)).strip() if title else ""
    description = soup.get_text(" ", strip=True) if BeautifulSoup else html_body
    return {"title": title_text, "description": description}


def normalize_payload(payload: dict[str, Any], url: str = "") -> Job:
    payload = validate_external_job(payload)
    adapter = choose_adapter(url, payload)
    return adapter.normalize(payload, url)


def canonical_job_key(job: Job) -> str:
    return identity_candidates(job)[0]


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if not parsed.netloc:
        return url.rstrip("/").lower()
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "").lower()).strip()


def identity_candidates(job: Job) -> list[str]:
    """Return stable identity values, retaining the historical API."""
    return [candidate.identity_value for candidate in identity_records(job)]


def identity_records(job: Job) -> list[JobIdentityCandidate]:
    """Build namespaced strong identities and non-merging weak signals.

    Strong identities are safe for automatic consolidation. Weak identities are
    deliberately persisted only as duplicate signals and never select an
    existing job during ingestion.
    """
    raw = job.raw_payload or {}
    platform = _text(raw.get("site") or raw.get("source_platform") or raw.get("job_source"))
    namespace = f"{job.source}:{platform}" if platform else job.source
    records: list[JobIdentityCandidate] = []
    if job.external_id:
        records.append(JobIdentityCandidate("source_external_id", f"source:{namespace}:{job.external_id}", "strong", job.source))
    requisition_id = _text(raw.get("requisition_id") or raw.get("requisitionId"))
    if requisition_id and requisition_id != job.external_id:
        records.append(JobIdentityCandidate("requisition_id", f"requisition:{namespace}:{requisition_id}", "strong", job.source))
    url = _canonical_url(job.url or _text(raw.get("job_url") or raw.get("hostedUrl") or raw.get("jobUrl")))
    if url:
        records.append(JobIdentityCandidate("canonical_url", f"url:{url}", "strong", job.source))
    text_key = _normalized_text(f"{job.company}|{job.title}|{job.location}")
    description_key = hashlib.sha256(_normalized_text(job.description).encode("utf-8")).hexdigest()
    if text_key.strip("|"):
        records.append(JobIdentityCandidate("company_title_location", f"text:{text_key}", "weak", job.source))
    if job.description:
        records.append(JobIdentityCandidate("description_fingerprint", f"description:{description_key}", "weak", job.source))
    return records or [JobIdentityCandidate("generated", f"generated:{job.id}", "weak", job.source)]
