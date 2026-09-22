"""ATS-specific inspection adapters over the generic form contracts.

Adapters recognize provider structure and enrich semantic metadata. They do
not resolve answers, mutate pages or execute browser actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .inspector import ATSInspector, DOMFieldBinding, FormBindings, InspectedForm, InspectionError
from .models import ApplicationField, ApplicationForm


@dataclass
class AdapterInspectionResult:
    provider: str
    confidence: float
    form: ApplicationForm
    bindings: FormBindings
    warnings: list[str] = field(default_factory=list)
    unsupported_features: list[str] = field(default_factory=list)

    @property
    def form_bindings(self) -> FormBindings:
        """Compatibility alias for callers that use the domain name."""
        return self.bindings


class ATSAdapter(Protocol):
    provider: str

    def matches(self, url: str = "", html: str = "") -> bool: ...

    def confidence(self, url: str = "", html: str = "") -> float: ...

    def allowed_hosts(self, url: str) -> set[str]: ...

    def locate_application_root(self, html: str) -> str: ...

    def inspect(self, html: str, url: str = "", form_id: str = "application") -> AdapterInspectionResult: ...


_SEMANTIC_ALIASES: dict[str, set[str]] = {
    "first_name": {"first_name", "firstname", "given_name"},
    "last_name": {"last_name", "lastname", "family_name", "surname"},
    "full_name": {"full_name", "fullname", "name"},
    "email": {"email", "email_address"},
    "phone": {"phone", "phone_number", "mobile", "mobile_phone"},
    "location": {"location", "city", "current_location"},
    "linkedin": {"linkedin", "linkedin_profile", "linkedin_url"},
    "github": {"github", "github_profile", "github_url"},
    "portfolio": {"portfolio", "portfolio_url", "website", "personal_website"},
    "resume": {"resume", "resume_file", "cv", "cv_file"},
    "cover_letter": {"cover_letter", "coverletter"},
    "work_authorization": {"authorized_to_work", "work_authorization", "right_to_work", "work_permit"},
    "requires_sponsorship": {"sponsorship", "requires_sponsorship", "visa_sponsorship", "need_sponsorship"},
    "salary_expectation": {"salary", "salary_expectation", "desired_salary", "compensation"},
    "notice_period": {"notice_period", "availability", "start_date"},
    "relocation": {"relocation", "willing_to_relocate"},
    "gender": {"gender", "sex"},
    "race_ethnicity": {"race", "ethnicity", "race_ethnicity"},
    "veteran_status": {"veteran", "veteran_status"},
    "disability": {"disability", "disability_status", "accommodation"},
}

_LABEL_ALIASES: dict[str, set[str]] = {
    "first_name": {"first name", "given name"},
    "last_name": {"last name", "family name", "surname"},
    "full_name": {"full name", "name"},
    "email": {"email", "email address", "e mail"},
    "phone": {"phone", "phone number", "mobile phone"},
    "location": {"location", "city", "current location"},
    "linkedin": {"linkedin profile", "linkedin url", "linkedin"},
    "github": {"github profile", "github url", "github"},
    "portfolio": {"portfolio", "portfolio url", "personal website", "website"},
    "resume": {"resume", "resume upload", "cv", "curriculum vitae"},
    "cover_letter": {"cover letter", "cover letter upload"},
    "work_authorization": {"authorized to work", "work authorization", "right to work"},
    "requires_sponsorship": {
        "will you now or in the future require sponsorship",
        "will you require sponsorship",
        "require sponsorship",
        "visa sponsorship",
    },
    "salary_expectation": {"salary expectation", "desired salary", "salary requirements"},
    "notice_period": {"notice period", "availability", "when can you start"},
    "relocation": {"willing to relocate", "relocation"},
    "gender": {"gender", "sex"},
    "race_ethnicity": {"race", "ethnicity", "race ethnicity"},
    "veteran_status": {"veteran status", "protected veteran"},
    "disability": {"disability", "disability status", "accommodation"},
}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _greenhouse_key(field: ApplicationField) -> tuple[str, bool]:
    match = re.search(r"job_application\[([^\]]+)\]", field.key.casefold())
    if match:
        return _normalize(match.group(1)), True
    return _normalize(field.key), False


def _semantic_match(field: ApplicationField) -> tuple[str, float, str]:
    key, namespaced = _greenhouse_key(field)
    for semantic_type, aliases in _SEMANTIC_ALIASES.items():
        if key in {_normalize(alias) for alias in aliases}:
            return semantic_type, 1.0 if namespaced else 0.95, "greenhouse_signature"
    label = _normalize(field.label)
    for semantic_type, aliases in _LABEL_ALIASES.items():
        if label in {_normalize(alias) for alias in aliases}:
            return semantic_type, 0.85, "greenhouse_label"
    return "unknown", 0.0, "unknown"


def _selector_for_form(form: Tag, soup: BeautifulSoup) -> str:
    if form.get("id"):
        value = str(form["id"]).replace("\\", "\\\\").replace('"', '\\"')
        return f"#{value}"
    if form.get("data-provider") == "greenhouse":
        return 'form[data-provider="greenhouse"]'
    forms = soup.find_all("form")
    if len(forms) == 1:
        return "form"
    parent = form.parent
    siblings = [item for item in parent.find_all("form", recursive=False)] if isinstance(parent, Tag) else forms
    index = next((position for position, item in enumerate(siblings, 1) if item is form), 1)
    return f"form:nth-of-type({index})"


class GreenhouseAdapter:
    provider = "greenhouse"

    def matches(self, url: str = "", html: str = "") -> bool:
        hostname = (urlparse(url).hostname or "").casefold()
        if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
            return True
        soup = BeautifulSoup(html, "html.parser")
        if soup.find(attrs={"data-provider": "greenhouse"}) or soup.find(attrs={"data-ats": "greenhouse"}):
            return True
        return bool(soup.find("form", id="application_form") and soup.select('form input[name^="job_application["]'))

    def confidence(self, url: str = "", html: str = "") -> float:
        hostname = (urlparse(url).hostname or "").casefold()
        if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
            return 1.0
        soup = BeautifulSoup(html, "html.parser")
        if soup.find(attrs={"data-provider": "greenhouse"}) or soup.find(attrs={"data-ats": "greenhouse"}):
            return 1.0
        if soup.find("form", id="application_form") and soup.select('form input[name^="job_application["]'):
            return 1.0
        if soup.select('input[name^="job_application["]'):
            return 0.95
        return 0.0

    def allowed_hosts(self, url: str) -> set[str]:
        hostname = (urlparse(url).hostname or "").casefold()
        if not hostname:
            return set()
        return {hostname}

    def locate_application_root(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")
        if not forms:
            raise InspectionError("GREENHOUSE_APPLICATION_ROOT_NOT_FOUND: no form element")
        candidates: list[tuple[int, Tag]] = []
        for form in forms:
            score = 0
            if form.get("id") == "application_form":
                score += 100
            if form.get("data-provider") == "greenhouse":
                score += 90
            if form.select('input[name^="job_application["]'):
                score += 80
            if score:
                candidates.append((score, form))
        if not candidates:
            raise InspectionError("GREENHOUSE_APPLICATION_ROOT_NOT_FOUND: Greenhouse form signature missing")
        highest = max(score for score, _ in candidates)
        roots = [form for score, form in candidates if score == highest]
        if len(roots) != 1:
            raise InspectionError("AMBIGUOUS_GREENHOUSE_APPLICATION_ROOT: multiple matching forms")
        return _selector_for_form(roots[0], soup)

    def inspect(self, html: str, url: str = "", form_id: str = "application") -> AdapterInspectionResult:
        if not self.matches(url, html):
            raise InspectionError("GREENHOUSE_SIGNATURE_NOT_FOUND")
        root_selector = self.locate_application_root(html)
        inspected = ATSInspector().inspect_html(html, url=url, form_id=form_id, form_selector=root_selector)
        return self.enrich_semantics(inspected, html, url)

    def enrich_semantics(self, inspected: InspectedForm, html: str = "", url: str = "") -> AdapterInspectionResult:
        inspected.form.provider = self.provider
        inspected.form.source = "greenhouse_adapter"
        for field in inspected.form.fields:
            semantic_type, confidence, source = _semantic_match(field)
            if semantic_type != "unknown":
                field.semantic_type = semantic_type
                field.confidence = confidence
                field.source = source
            else:
                field.confidence = 0.0
                field.source = "greenhouse_unknown"
        soup = BeautifulSoup(html, "html.parser")
        unsupported: list[str] = []
        if soup.select('[role="combobox"]'):
            unsupported.append("custom_combobox")
        if soup.select('[contenteditable="true"]'):
            unsupported.append("contenteditable_control")
        warnings: list[str] = []
        if any(field.confidence < 0.70 and field.required for field in inspected.form.fields):
            warnings.append("required field has no high-confidence semantic mapping")
        return AdapterInspectionResult(self.provider, self.confidence(url, html), inspected.form, inspected.bindings, warnings, unsupported)


ADAPTERS: tuple[ATSAdapter, ...] = (GreenhouseAdapter(),)


def adapter_for(url: str = "", html: str = "") -> ATSAdapter | None:
    for adapter in ADAPTERS:
        if adapter.matches(url, html):
            return adapter
    return None


def inspect_with_adapter(html: str, url: str = "", form_id: str = "application") -> AdapterInspectionResult:
    adapter = adapter_for(url, html)
    if adapter is None:
        raise InspectionError("NO_ATS_ADAPTER_MATCH")
    return adapter.inspect(html, url, form_id)
