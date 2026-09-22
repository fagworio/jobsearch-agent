"""Read-only DOM inspection into ATS-neutral form contracts.

The inspector returns the domain form and browser-specific bindings separately.
It never resolves answers, fills controls or clicks a submit control.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .models import ApplicationField, ApplicationForm, ValidationResult


@dataclass
class DOMFieldBinding:
    field_key: str
    locator: str
    control: str
    option_locators: dict[str, str] = field(default_factory=dict)
    option_values: dict[str, str] = field(default_factory=dict)
    listbox_id: str = ""
    autocomplete: str = ""
    multiple: bool = False


@dataclass
class FormBindings:
    form_id: str
    fields: list[DOMFieldBinding] = field(default_factory=list)
    root_locator: str = ""

    def for_field(self, field_key: str) -> DOMFieldBinding | None:
        return next((binding for binding in self.fields if binding.field_key == field_key), None)


class InspectionError(ValueError):
    pass


@dataclass
class InspectedForm:
    form: ApplicationForm
    bindings: FormBindings


def detect_provider(url: str = "", html: str = "") -> str:
    hostname = (urlparse(url).hostname or "").casefold()
    if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
        return "greenhouse"
    if hostname == "lever.co" or hostname.endswith(".lever.co"):
        return "lever"
    if hostname == "ashbyhq.com" or hostname.endswith(".ashbyhq.com"):
        return "ashby"
    soup = BeautifulSoup(html, "html.parser")
    provider = soup.find(attrs={"data-provider": True})
    if provider and str(provider.get("data-provider")).casefold() in {"greenhouse", "lever", "ashby"}:
        return str(provider["data-provider"]).casefold()
    return "generic"


def compute_form_fingerprint(form: ApplicationForm, bindings: FormBindings) -> str:
    payload = {
        "form_id": form.form_id,
        "provider": form.provider,
        "fields": [{"key": item.key, "label": item.label, "field_type": item.field_type.casefold().strip(), "semantic_type": item.semantic_type, "semantic_context": item.semantic_context, "confidence": item.confidence, "source": item.source, "required": item.required, "options": item.options, "disabled": item.disabled} for item in sorted(form.fields, key=lambda item: item.key)],
        "bindings": [
            {
                "field_key": item.field_key,
                "locator": item.locator,
                "control": item.control,
                "option_values": item.option_values,
                "listbox_id": item.listbox_id,
                "autocomplete": item.autocomplete,
                "multiple": item.multiple,
            }
            for item in sorted(bindings.fields, key=lambda item: item.field_key)
        ],
        "root_locator": bindings.root_locator,
        "capability_issues": [issue.__dict__ for issue in form.capability_issues],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_form_bindings(form: ApplicationForm, bindings: FormBindings):
    from .models import ValidationResult

    errors: list[str] = []
    field_map = {field.key: field for field in form.fields}
    keys = [binding.field_key for binding in bindings.fields]
    if bindings.form_id != form.form_id:
        errors.append("binding form_id does not match form")
    if len(keys) != len(set(keys)):
        errors.append("binding field_key is duplicated")
    if set(keys) != set(field_map):
        errors.append("bindings and form fields do not have the same keys")
    compatible = {
        "text": {"text"}, "textarea": {"textarea"}, "email": {"email", "text"},
        "tel": {"tel", "text"}, "url": {"url", "text"}, "date": {"date", "text"},
        "select": {"select"}, "combobox": {"combobox"}, "radio": {"radio"},
        "checkbox": {"checkbox"}, "file": {"file"},
    }
    for binding in bindings.fields:
        field = field_map.get(binding.field_key)
        if field is None:
            continue
        if not binding.locator.strip() or not binding.control.strip():
            errors.append(f"binding is empty: {binding.field_key}")
        if any(token in f"{binding.control} {binding.locator}".casefold() for token in ("submit", "button")):
            errors.append(f"binding targets submit/button: {binding.field_key}")
        field_type = field.field_type.casefold().strip()
        if binding.control.casefold() not in compatible.get(field_type, set()):
            errors.append(f"binding control does not match field type: {binding.field_key}")
        if set(binding.option_values) != set(field.options):
            errors.append(f"binding options do not match field options: {binding.field_key}")
    return ValidationResult(not errors, "OK" if not errors else "INVALID_FORM_BINDINGS", errors)


def validate_bindings_against_html(form: ApplicationForm, bindings: FormBindings, html: str, url: str = ""):
    from .models import ValidationResult

    static = validate_form_bindings(form, bindings)
    errors = list(static.errors)
    soup = BeautifulSoup(html, "html.parser")
    try:
        current = _reinspect_form(form, html, url, bindings.root_locator or None)
        if compute_form_fingerprint(current.form, current.bindings) != compute_form_fingerprint(form, bindings):
            errors.append("current DOM fingerprint does not match inspected form")
    except InspectionError as exc:
        errors.append(str(exc))
    for binding in bindings.fields:
        try:
            if len(soup.select(binding.locator)) != 1:
                errors.append(f"locator must match exactly one element: {binding.field_key}")
        except Exception as exc:
            errors.append(f"invalid locator for {binding.field_key}: {exc}")
        for label, locator in binding.option_locators.items():
            try:
                if len(soup.select(locator)) != 1:
                    errors.append(f"option locator must match exactly one element: {binding.field_key}:{label}")
            except Exception as exc:
                errors.append(f"invalid option locator for {binding.field_key}:{label}: {exc}")
    return ValidationResult(not errors, "OK" if not errors else "STALE_FORM_BINDINGS", errors)


def fingerprint_html(form: ApplicationForm, bindings: FormBindings, html: str, url: str = "") -> str:
    current = _reinspect_form(form, html, url, bindings.root_locator or None)
    return compute_form_fingerprint(current.form, current.bindings)


def _reinspect_form(form: ApplicationForm, html: str, url: str, form_selector: str | None) -> InspectedForm:
    """Reapply provider enrichment so semantic fingerprints remain comparable."""
    if form.provider and form.provider != "generic":
        from .ats import ADAPTERS

        adapter = next((item for item in ADAPTERS if item.provider == form.provider), None)
        if adapter:
            return adapter.inspect(html, url=url, form_id=form.form_id)
    return ATSInspector().inspect_html(html, url=url, form_id=form.form_id, form_selector=form_selector)


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _key(element: Tag, index: int) -> str:
    return str(element.get("name") or element.get("id") or f"field-{index}")


def _label_for(soup: BeautifulSoup, element: Tag) -> str:
    element_id = element.get("id")
    if element_id:
        label = soup.find("label", attrs={"for": element_id})
        if label:
            return label.get_text(" ", strip=True)
    parent = element.find_parent("label")
    if parent:
        return parent.get_text(" ", strip=True)
    return str(element.get("aria-label") or element.get("placeholder") or element.get("name") or element.get("id") or "")


def _required(element: Tag) -> bool:
    return element.has_attr("required") or str(element.get("aria-required", "")).casefold() == "true"


def _disabled(element: Tag) -> bool:
    return element.has_attr("disabled") or str(element.get("aria-disabled", "")).casefold() == "true"


def _ancestor_locator(element: Tag) -> str:
    parts: list[str] = []
    current: Tag | None = element
    while current is not None and current.name not in {"[document]", "html"}:
        if current.get("id"):
            parts.append(f'#{_css_escape(str(current["id"]))}')
            break
        siblings = [item for item in current.parent.find_all(current.name, recursive=False)] if current.parent else [current]
        position = next((index + 1 for index, item in enumerate(siblings) if item is current), 1)
        parts.append(f"{current.name}:nth-of-type({position})")
        current = current.parent
    return " > ".join(reversed(parts))


def _base_locator(element: Tag) -> str:
    if element.get("id"):
        return f'#{_css_escape(str(element["id"]))}'
    return _ancestor_locator(element)


def _option_locator(element: Tag, index: int, group_locator: str) -> str:
    if element.get("id"):
        return f'#{_css_escape(str(element["id"]))}'
    if element.get("name") and element.get("value") is not None:
        return f'{element.name}[name="{_css_escape(str(element["name"]))}"][value="{_css_escape(str(element["value"]))}"]'
    return _ancestor_locator(element)


class ATSInspector:
    """Build a read-only form snapshot from HTML or a Playwright page."""

    def inspect_html(self, html: str, url: str = "", form_id: str = "inspected-form", form_selector: str | None = None) -> InspectedForm:
        soup = BeautifulSoup(html, "html.parser")
        root = self._select_root(soup, form_selector)
        controls = [element for element in root.find_all(["input", "textarea", "select"]) if self._is_data_control(element)]
        # The international-phone widget inserts its own country search input;
        # it is an implementation detail of the phone field, not an application
        # answer field and must not become a second candidate identity field.
        controls = [element for element in controls if not (element.get("role") == "combobox" and "iti__search-input" in element.get("class", []))]
        grouped: dict[str, list[Tag]] = defaultdict(list)
        for index, element in enumerate(controls):
            grouped[self._group_key(element, index)].append(element)

        fields: list[ApplicationField] = []
        bindings: list[DOMFieldBinding] = []
        used_keys: dict[str, int] = defaultdict(int)
        for index, group in enumerate(grouped.values()):
            first = group[0]
            field_key = _key(first, index)
            used_keys[field_key] += 1
            if used_keys[field_key] > 1:
                field_key = f"{field_key}-{used_keys[field_key]}"
            field_type, semantic_type, options, multiple, accepted_types = self._describe_group(root, group)
            label = _label_for(root, first)
            field = ApplicationField(
                key=field_key,
                label=label,
                field_type=field_type,
                semantic_type=semantic_type,
                required=any(_required(element) for element in group),
                options=options,
                source="dom_inspector",
                multiple=multiple,
                disabled=all(_disabled(element) for element in group),
                accepted_types=accepted_types,
            )
            fields.append(field)
            bindings.append(self._binding(field_key, field_type, group, options))

        provider = detect_provider(url, html)
        form = ApplicationForm(form_id=form_id, provider=provider, fields=fields, source="dom_inspector")
        root_locator = _base_locator(root) if root.name == "form" else ""
        return InspectedForm(form, FormBindings(form_id, bindings, root_locator))

    def inspect_page(self, page: Any, url: str = "", form_id: str = "inspected-form", form_selector: str | None = None) -> InspectedForm:
        """Read page HTML only; the page is never mutated."""
        return self.inspect_html(page.content(), url or str(getattr(page, "url", "")), form_id, form_selector)

    @staticmethod
    def _select_root(soup: BeautifulSoup, form_selector: str | None) -> Tag:
        if form_selector:
            matches = soup.select(form_selector)
            if len(matches) != 1:
                raise InspectionError("AMBIGUOUS_FORM: form_selector must match exactly one root")
            return matches[0]
        forms = soup.find_all("form")
        if len(forms) > 1:
            raise InspectionError("AMBIGUOUS_FORM: multiple form roots require form_selector")
        return forms[0] if forms else soup

    @staticmethod
    def _is_data_control(element: Tag) -> bool:
        if element.name != "input":
            return True
        return str(element.get("type", "text")).casefold() not in {"hidden", "submit", "button", "reset", "image"}

    @staticmethod
    def _group_key(element: Tag, index: int) -> str:
        field_type = str(element.get("type", "text")).casefold()
        name = str(element.get("name") or "")
        if field_type in {"radio", "checkbox"} and name:
            return f"{field_type}:{name}"
        return f"single:{index}"

    @staticmethod
    def _describe_group(soup: BeautifulSoup, group: list[Tag]) -> tuple[str, str, list[str], bool, list[str]]:
        first = group[0]
        if first.get("role") == "combobox":
            is_multiple = first.has_attr("multiple") or str(first.get("aria-multiselectable", "")).casefold() == "true"
            return "combobox", "unknown", [], is_multiple, []
        if first.name == "textarea":
            return "textarea", "unknown", [], False, []
        if first.name == "select":
            return "select", "unknown", [option.get_text(" ", strip=True) or str(option.get("value", "")) for option in first.find_all("option")], bool(first.has_attr("multiple")), []
        input_type = str(first.get("type", "text")).casefold()
        if input_type == "radio":
            return "radio", "unknown", [_label_for(soup, element) or str(element.get("value", "")) for element in group], False, []
        if input_type == "checkbox":
            if len(group) > 1:
                return "checkbox", "checkbox_multi", [_label_for(soup, element) or str(element.get("value", "")) for element in group], True, []
            return "checkbox", "checkbox_boolean", [], False, []
        accepted_types = [item.strip() for item in str(first.get("accept", "")).split(",") if item.strip()] if input_type == "file" else []
        return input_type if input_type in {"email", "tel", "url", "date", "file", "text"} else "text", "unknown", [], False, accepted_types

    @staticmethod
    def _binding(field_key: str, field_type: str, group: list[Tag], options: list[str]) -> DOMFieldBinding:
        first = group[0]
        locator = _base_locator(first)
        option_locators: dict[str, str] = {}
        option_values: dict[str, str] = {}
        if first.name == "select":
            for option in first.find_all("option"):
                label = option.get_text(" ", strip=True) or str(option.get("value", ""))
                value = _css_escape(str(option.get("value", "")))
                option_locators[label] = f'{locator} option[value="{value}"]'
                option_values[label] = str(option.get("value", ""))
        elif field_type == "combobox":
            controls = str(first.get("aria-controls", "") or first.get("aria-owns", "")).split()
            listbox_id = controls[0] if len(controls) == 1 else ""
            return DOMFieldBinding(
                field_key,
                locator,
                field_type,
                {},
                {},
                listbox_id,
                str(first.get("aria-autocomplete", "")).casefold(),
                first.has_attr("multiple")
                or str(first.get("aria-multiselectable", "")).casefold() == "true",
            )
        else:
            for index, (option, element) in enumerate(zip(options, group)):
                option_value = str(element.get("value", ""))
                option_values[option] = option_value
                option_locators[option] = _option_locator(element, index, locator)
        return DOMFieldBinding(field_key, locator, field_type, option_locators, option_values)
