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
from .models import ApplicationField, ApplicationForm, FormCapabilityIssue


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

    #: Recursos de terceiros que o board carrega e que sao leitura pura. Sem o
    #: script do reCAPTCHA Enterprise a pagina nao completa performAssessment()
    #: e o submit nunca dispara. Isto NAO resolve nem contorna desafio: apenas
    #: deixa a propria pagina executar o fluxo normal dela.
    resource_hosts = (
        "www.recaptcha.net",
        "recaptcha.net",
        "www.google.com",
        "apis.google.com",
        "accounts.google.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "recruiting.cdn.greenhouse.io",
        "my.greenhouse.io",
        "www.dropbox.com",
        # Storage do board. A rota precisa permitir o host para a requisicao
        # chegar ao NetworkWriteGuard; la o POST so passa com permissao.
        "*.s3.amazonaws.com",
    )

    #: Storage do board, para onde o curriculo e enviado por POST (xhr). O
    #: bucket varia por regiao, dai o padrao de host. So e autorizado durante
    #: uma submissao autorizada, com orcamento proprio.
    upload_write_origins = ("*.s3.amazonaws.com",)

    def resource_allowed_hosts(self, url: str) -> set[str]:
        return set(self.resource_hosts)

    def upload_write_origins_for(self, url: str) -> tuple[str, ...]:
        return self.upload_write_origins

    def allowed_hosts(self, url: str) -> set[str]: ...

    def locate_application_root(self, html: str) -> str: ...

    def inspect(self, html: str, url: str = "", form_id: str = "application") -> AdapterInspectionResult: ...


_SEMANTIC_ALIASES: dict[str, set[str]] = {
    "first_name": {"first_name", "firstname", "given_name"},
    "last_name": {"last_name", "lastname", "family_name", "surname"},
    "preferred_first_name": {"preferred_first_name", "preferred_name", "chosen_name"},
    "full_name": {"full_name", "fullname", "name"},
    "email": {"email", "email_address"},
    "phone": {"phone", "phone_number", "mobile", "mobile_phone"},
    "country": {"country", "country_code", "country_of_residence"},
    "current_location": {"location", "city", "current_location"},
    "timezone": {"timezone", "time_zone", "preferred_timezone"},
    "preferred_relocation_location": {"relocation_location", "preferred_relocation_location"},
    "linkedin": {"linkedin", "linkedin_profile", "linkedin_url"},
    "github": {"github", "github_profile", "github_url"},
    "website": {"website", "personal_website", "personal_site", "website_url"},
    "portfolio": {"portfolio", "portfolio_url", "website", "personal_website"},
    "resume": {"resume", "resume_file", "cv", "cv_file"},
    "cover_letter": {"cover_letter", "coverletter"},
    "work_authorization": {"authorized_to_work", "authorized_to_work_in_brazil", "work_authorization", "right_to_work", "work_permit"},
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
    "preferred_first_name": {"preferred first name", "preferred name"},
    "full_name": {"full name", "name"},
    "email": {"email", "email address", "e mail"},
    "phone": {"phone", "phone number", "mobile phone"},
    "country": {"country", "country of residence", "country/region"},
    "current_location": {"location", "city", "current location"},
    "timezone": {"time zone", "timezone", "preferred time zone", "preferred timezone"},
    "preferred_relocation_location": {"preferred relocation location", "relocation location"},
    "linkedin": {"linkedin profile", "linkedin url", "linkedin"},
    "github": {"github profile", "github url", "github"},
    "website": {"personal website", "personal site", "website"},
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


_COUNTRY_ALIASES = {
    "brazil": "Brazil",
    "brasil": "Brazil",
    "united states": "United States",
    "usa": "United States",
    "u s": "United States",
    "canada": "Canada",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "portugal": "Portugal",
}


def _country_context(field: ApplicationField) -> dict[str, str]:
    text = f"{field.key} {field.label}".casefold()
    for alias, country in sorted(_COUNTRY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text):
            return {"country": country}
    return {}


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
        legacy_form = soup.find("form", id="application_form")
        modern_form = soup.find("form", id="application-form")
        namespaced_controls = soup.select(
            'form input[name^="job_application["], form select[name^="job_application["], form textarea[name^="job_application["]'
        )
        if legacy_form and namespaced_controls:
            return True
        if modern_form is not None and modern_form.select("input, select, textarea"):
            # O Lever usa o mesmo id="application-form". Sem um sinal adicional
            # do Greenhouse (controles namespaced ou host greenhouse.io, ja
            # tratado acima), o formulario e do outro provider.
            return bool(namespaced_controls)
        return False

    def confidence(self, url: str = "", html: str = "") -> float:
        hostname = (urlparse(url).hostname or "").casefold()
        if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
            return 1.0
        soup = BeautifulSoup(html, "html.parser")
        if soup.find(attrs={"data-provider": "greenhouse"}) or soup.find(attrs={"data-ats": "greenhouse"}):
            return 1.0
        if soup.find("form", id="application_form") and soup.select(
            'form input[name^="job_application["], form select[name^="job_application["], form textarea[name^="job_application["]'
        ):
            return 1.0
        modern_form = soup.find("form", id="application-form")
        if modern_form and modern_form.select("input, select, textarea"):
            return 1.0 if soup.select('form input[name^="job_application["]') else 0.0
        if soup.select(
            'input[name^="job_application["], select[name^="job_application["], textarea[name^="job_application["]'
        ):
            return 0.95
        return 0.0

    #: Recursos de terceiros que o board carrega e que sao leitura pura. Sem o
    #: script do reCAPTCHA Enterprise a pagina nao completa performAssessment()
    #: e o submit nunca dispara. Isto NAO resolve nem contorna desafio: apenas
    #: deixa a propria pagina executar o fluxo normal dela.
    resource_hosts = (
        "www.recaptcha.net",
        "recaptcha.net",
        "www.google.com",
        "apis.google.com",
        "accounts.google.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "recruiting.cdn.greenhouse.io",
        "my.greenhouse.io",
        "www.dropbox.com",
        # Storage do board. A rota precisa permitir o host para a requisicao
        # chegar ao NetworkWriteGuard; la o POST so passa com permissao.
        "*.s3.amazonaws.com",
    )

    #: Storage do board, para onde o curriculo e enviado por POST (xhr). O
    #: bucket varia por regiao, dai o padrao de host. So e autorizado durante
    #: uma submissao autorizada, com orcamento proprio.
    upload_write_origins = ("*.s3.amazonaws.com",)

    def resource_allowed_hosts(self, url: str) -> set[str]:
        return set(self.resource_hosts)

    def upload_write_origins_for(self, url: str) -> tuple[str, ...]:
        return self.upload_write_origins

    def allowed_hosts(self, url: str) -> set[str]:
        hostname = (urlparse(url).hostname or "").casefold()
        if not hostname:
            return set()
        hosts = {hostname}
        suffix = ".greenhouse.io"
        if hostname.endswith(suffix) and hostname != "greenhouse.io":
            prefix = hostname[: -len(suffix)]
            hosts.add(f"{prefix}.cdn.greenhouse.io")
            hosts.update({"boards.greenhouse.io", "boards.cdn.greenhouse.io"})
        return hosts

    def locate_application_root(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")
        if not forms:
            raise InspectionError("GREENHOUSE_APPLICATION_ROOT_NOT_FOUND: no form element")
        candidates: list[tuple[int, Tag]] = []
        for form in forms:
            score = 0
            if form.get("id") in {"application_form", "application-form"}:
                score += 100
            if form.get("data-provider") == "greenhouse":
                score += 90
            if form.select(
                'input[name^="job_application["], select[name^="job_application["], textarea[name^="job_application["]'
            ):
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
                field.semantic_context = _country_context(field) if semantic_type == "work_authorization" else {}
            else:
                field.confidence = 0.0
                field.source = "greenhouse_unknown"
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one(inspected.bindings.root_locator) if inspected.bindings.root_locator else soup
        if root is None:
            root = soup
        unsupported: list[str] = []
        capability_issues: list[FormCapabilityIssue] = []
        unsupported_combos = []
        for control in root.select('[role="combobox"]'):
            # Greenhouse's phone widget adds an internal country-search combo;
            # the actual tel input remains the supported application field.
            if "iti__search-input" in control.get("class", []):
                continue
            binding = None
            for candidate in inspected.bindings.fields:
                if candidate.control != "combobox":
                    continue
                try:
                    matches = soup.select(candidate.locator)
                except Exception:
                    continue
                if len(matches) == 1 and matches[0] is control:
                    binding = candidate
                    break
            field = next(
                (item for item in inspected.form.fields if binding and item.key == binding.field_key),
                None,
            )
            if control.name != "input" or field is None:
                unsupported_combos.append("custom_combobox")
            elif field.multiple:
                unsupported_combos.append("combobox_multiple")
            elif field.semantic_type in {"current_location", "preferred_relocation_location"}:
                # Greenhouse location controls can invoke geolocation and have
                # provider-specific behavior; keep them fail-closed for v1.
                unsupported_combos.append("combobox_location")
            elif control.get("aria-autocomplete", "").casefold() not in {"", "none", "list", "both"}:
                unsupported_combos.append("combobox_autocomplete")
        if unsupported_combos:
            unsupported.append("custom_combobox")
            for issue in sorted(set(unsupported_combos)):
                capability_issues.append(FormCapabilityIssue(issue, "blocker", evidence="role=combobox"))
        if root.select('[contenteditable="true"]'):
            unsupported.append("contenteditable_control")
            capability_issues.append(FormCapabilityIssue("contenteditable_control", "blocker", evidence="contenteditable=true"))
        inspected.form.capability_issues = capability_issues
        warnings: list[str] = []
        if any(field.confidence < 0.70 and field.required for field in inspected.form.fields):
            warnings.append("required field has no high-confidence semantic mapping")
        return AdapterInspectionResult(self.provider, self.confidence(url, html), inspected.form, inspected.bindings, warnings, unsupported)



#: Nomes de wire que o Lever publica no proprio HTML do formulario.
_LEVER_SEMANTICS: dict[str, str] = {
    "name": "full_name",
    "email": "email",
    "phone": "phone",
    "location": "current_location",
    "selectedlocation": "current_location",
    "org": "current_company",
    "urls[linkedin]": "linkedin",
    "urls[github]": "github",
    "urls[twitter]": "website",
    "urls[portfolio]": "portfolio",
    "urls[website]": "website",
    "resume": "resume",
    "coverletter": "cover_letter",
}


class LeverAdapter:
    """Adapter do formulario HTML classico do Lever.

    Diferente do Greenhouse, o Lever renderiza um ``<form>`` tradicional com
    ``name`` reais (``name``, ``email``, ``org``, ``urls[LinkedIn]``) e o
    ``action`` aponta para o proprio ``/apply``.
    """

    provider = "lever"

    def matches(self, url: str = "", html: str = "") -> bool:
        hostname = (urlparse(url).hostname or "").casefold()
        if hostname.endswith("lever.co"):
            return True
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id="application-form")
        if form is None:
            return False
        return bool(form.select('input[name="resume"], input[name="org"], input[name="email"]'))

    def confidence(self, url: str = "", html: str = "") -> float:
        hostname = (urlparse(url).hostname or "").casefold()
        if hostname.endswith("lever.co"):
            return 1.0
        return 0.9 if self.matches(url, html) else 0.0

    def allowed_hosts(self, url: str) -> set[str]:
        hostname = (urlparse(url).hostname or "").casefold()
        hosts = {hostname} if hostname else set()
        hosts.update({"jobs.lever.co", "cdn.lever.co"})
        return hosts

    def locate_application_root(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        if soup.find("form", id="application-form"):
            return "#application-form"
        if soup.find("form"):
            return "form"
        raise InspectionError("LEVER_APPLICATION_ROOT_NOT_FOUND: no form element")

    def inspect(self, html: str, url: str = "", form_id: str = "application") -> AdapterInspectionResult:
        if not self.matches(url, html):
            raise InspectionError("LEVER_SIGNATURE_NOT_FOUND")
        inspected = ATSInspector().inspect_html(
            html, url=url, form_id=form_id, form_selector=self.locate_application_root(html)
        )
        inspected.form.provider = self.provider
        inspected.form.source = "lever_adapter"
        for field in inspected.form.fields:
            semantic = _LEVER_SEMANTICS.get(field.key.casefold().strip(), "")
            if semantic:
                field.semantic_type = semantic
                field.confidence = 1.0
                field.source = "lever_name"
            else:
                field.confidence = 0.0
                field.source = "lever_unknown"
        warnings = [
            "required field has no high-confidence semantic mapping"
        ] if any(field.confidence < 0.70 and field.required for field in inspected.form.fields) else []
        return AdapterInspectionResult(self.provider, self.confidence(url, html), inspected.form, inspected.bindings, warnings, [])


ADAPTERS: tuple[ATSAdapter, ...] = (GreenhouseAdapter(), LeverAdapter())


def adapter_for(url: str = "", html: str = "") -> ATSAdapter | None:
    """Escolhe o adapter priorizando o host da URL.

    Greenhouse e Lever usam o mesmo ``id="application-form"``, entao decidir
    pela assinatura de HTML fazia o adapter do Greenhouse reivindicar o
    formulario do Lever. O host da URL e o sinal mais forte; a assinatura de
    HTML fica como fallback para URLs genericas.
    """
    for adapter in ADAPTERS:
        if adapter.matches(url, ""):
            return adapter
    for adapter in ADAPTERS:
        if adapter.matches(url, html):
            return adapter
    return None


def inspect_with_adapter(html: str, url: str = "", form_id: str = "application") -> AdapterInspectionResult:
    adapter = adapter_for(url, html)
    if adapter is None:
        raise InspectionError("NO_ATS_ADAPTER_MATCH")
    return adapter.inspect(html, url, form_id)
