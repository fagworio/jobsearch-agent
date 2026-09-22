"""Read-only LinkedIn fixture inspection.

This module accepts HTML snapshots only. It never opens a page, logs in,
clicks controls, fills forms, follows navigation, or submits applications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
import unicodedata

from bs4 import BeautifulSoup

from ..inspector import ATSInspector, FormBindings, InspectionError
from ..models import ApplicationField, ApplicationForm
from .session import LinkedInAuthState


class LinkedInApplyClassification(StrEnum):
    EASY_APPLY = "EASY_APPLY"
    EXTERNAL_APPLY = "EXTERNAL_APPLY"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"


@dataclass
class LinkedInInspectionResult:
    classification: LinkedInApplyClassification
    form: ApplicationForm | None = None
    bindings: FormBindings | None = None
    auth_state: LinkedInAuthState | None = None
    warnings: list[str] = field(default_factory=list)


_COUNTRY_ALIASES = {
    "brazil": "Brazil",
    "brasil": "Brazil",
    "canada": "Canada",
    "united states": "United States",
    "usa": "United States",
    "united kingdom": "United Kingdom",
    "portugal": "Portugal",
}


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", plain.casefold()).strip()


def _country_context(label: str) -> dict[str, str]:
    normalized = _normalize(label)
    for alias, country in sorted(_COUNTRY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(_normalize(alias))}(?![a-z])", normalized):
            return {"country": country}
    return {}


def _auth_state(soup: BeautifulSoup, text: str) -> LinkedInAuthState | None:
    if soup.select_one('iframe[src*="captcha"], [data-testid*="captcha"], [id*="captcha"]') or "captcha" in text:
        return LinkedInAuthState.NEEDS_CAPTCHA
    if any(term in text for term in ("two step verification", "two factor", "verification code", "security code")):
        return LinkedInAuthState.NEEDS_MFA
    if soup.select_one('input[name="session_key"], input[name="session_password"]') or any(
        term in text for term in ("sign in", "log in", "join now")
    ):
        return LinkedInAuthState.NEEDS_LOGIN
    return None


class LinkedInInspector:
    """Classify and inspect synthetic/local HTML without live LinkedIn access."""

    provider = "linkedin"

    def classify_html(self, html: str) -> LinkedInApplyClassification:
        soup = BeautifulSoup(html, "html.parser")
        text = _normalize(soup.get_text(" ", strip=True))
        if _auth_state(soup, text):
            return LinkedInApplyClassification.AUTH_REQUIRED
        if any(term in text for term in ("already applied", "application submitted", "you applied")):
            return LinkedInApplyClassification.ALREADY_APPLIED
        if any(term in text for term in ("no longer available", "no longer accepting", "job is closed", "position is closed")):
            return LinkedInApplyClassification.UNAVAILABLE
        if self._easy_apply_control(soup):
            return LinkedInApplyClassification.EASY_APPLY
        if self._external_apply_control(soup):
            return LinkedInApplyClassification.EXTERNAL_APPLY
        return LinkedInApplyClassification.UNAVAILABLE

    def inspect_html(self, html: str, form_id: str = "linkedin-application") -> LinkedInInspectionResult:
        soup = BeautifulSoup(html, "html.parser")
        text = _normalize(soup.get_text(" ", strip=True))
        auth_state = _auth_state(soup, text)
        if auth_state:
            return LinkedInInspectionResult(LinkedInApplyClassification.AUTH_REQUIRED, auth_state=auth_state)

        classification = self.classify_html(html)
        if classification != LinkedInApplyClassification.EASY_APPLY:
            warning = "no apply control or Easy Apply form" if classification == LinkedInApplyClassification.UNAVAILABLE else "form inspection is not applicable"
            return LinkedInInspectionResult(classification, warnings=[warning])

        form_selector = self._easy_form_selector(soup)
        inspected = ATSInspector().inspect_html(html, form_id=form_id, form_selector=form_selector)
        inspected.form.provider = self.provider
        inspected.form.source = "linkedin_fixture_inspector"
        for field in inspected.form.fields:
            self._enrich_field(field)
        return LinkedInInspectionResult(classification, inspected.form, inspected.bindings)

    @staticmethod
    def _easy_apply_control(soup: BeautifulSoup) -> bool:
        controls = soup.select("button, a, [role=button]")
        return any("easy apply" in _normalize(control.get_text(" ", strip=True) + " " + str(control.get("aria-label", ""))) for control in controls) or bool(soup.select('[data-linkedin="easy-apply"]'))

    @staticmethod
    def _external_apply_control(soup: BeautifulSoup) -> bool:
        controls = soup.select("button, a, [role=button]")
        return any(any(term in _normalize(control.get_text(" ", strip=True)) for term in ("apply on company website", "apply on employer site", "external apply")) for control in controls)

    @staticmethod
    def _easy_form_selector(soup: BeautifulSoup) -> str | None:
        marked = soup.select('form[data-linkedin="easy-apply"]')
        if len(marked) == 1:
            return 'form[data-linkedin="easy-apply"]'
        forms = soup.find_all("form")
        if len(forms) == 1 and forms[0].get("id"):
            return f'#{forms[0]["id"]}'
        if len(forms) == 1:
            return "form"
        raise InspectionError("AMBIGUOUS_LINKEDIN_EASY_APPLY_FORM")

    @staticmethod
    def _enrich_field(field: ApplicationField) -> None:
        label = field.label
        normalized = _normalize(f"{field.key} {label}")
        experience = re.search(r"years\s+(?:of|with)\s+(.+?)(?:\s+experience\b|\?|$)", label, re.IGNORECASE)
        if experience:
            skill = re.sub(r"\s+", " ", experience.group(1)).strip(" .")
            field.semantic_type = "experience_years"
            field.semantic_context = {"skill": skill}
            field.confidence = 0.95
            field.source = "linkedin_label"
            return
        mappings = (
            ("email", "email"),
            ("phone", "phone"),
            ("mobile", "phone"),
            ("resume", "resume"),
            ("curriculum vitae", "resume"),
            ("authorized to work", "work_authorization"),
            ("work authorization", "work_authorization"),
            ("sponsorship", "requires_sponsorship"),
            ("first name", "first_name"),
            ("last name", "last_name"),
        )
        for term, semantic_type in mappings:
            if term in normalized:
                field.semantic_type = semantic_type
                field.confidence = 0.95
                field.source = "linkedin_label"
                if semantic_type == "work_authorization":
                    field.semantic_context = _country_context(label)
                return
        if field.field_type == "file":
            field.semantic_type = "resume"
            field.confidence = 0.9
            field.source = "linkedin_file_control"
