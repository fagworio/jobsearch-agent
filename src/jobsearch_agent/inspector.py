"""Read-only DOM inspection into ATS-neutral form contracts.

The inspector returns the domain form and browser-specific bindings separately.
It never resolves answers, fills controls or clicks a submit control.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from .models import ApplicationField, ApplicationForm


@dataclass
class DOMFieldBinding:
    field_key: str
    locator: str
    control: str
    option_locators: dict[str, str] = field(default_factory=dict)


@dataclass
class FormBindings:
    form_id: str
    fields: list[DOMFieldBinding] = field(default_factory=list)

    def for_field(self, field_key: str) -> DOMFieldBinding | None:
        return next((binding for binding in self.fields if binding.field_key == field_key), None)


@dataclass
class InspectedForm:
    form: ApplicationForm
    bindings: FormBindings


def detect_provider(url: str = "", html: str = "") -> str:
    haystack = f"{url} {html}".casefold()
    if "greenhouse" in haystack:
        return "greenhouse"
    if "lever.co" in haystack or "jobs.lever" in haystack:
        return "lever"
    if "ashby" in haystack:
        return "ashby"
    return "generic"


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


def _base_locator(element: Tag) -> str:
    if element.get("id"):
        return f'#{_css_escape(str(element["id"]))}'
    if element.get("name"):
        tag = element.name
        return f'{tag}[name="{_css_escape(str(element["name"]))}"]'
    return f"{element.name}"


class ATSInspector:
    """Build a read-only form snapshot from HTML or a Playwright page."""

    def inspect_html(self, html: str, url: str = "", form_id: str = "inspected-form") -> InspectedForm:
        soup = BeautifulSoup(html, "html.parser")
        controls = [element for element in soup.find_all(["input", "textarea", "select"]) if self._is_data_control(element)]
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
            field_type, semantic_type, options, multiple, accepted_types = self._describe_group(soup, group)
            label = _label_for(soup, first)
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
        return InspectedForm(form, FormBindings(form_id, bindings))

    def inspect_page(self, page: Any, url: str = "", form_id: str = "inspected-form") -> InspectedForm:
        """Read page HTML only; the page is never mutated."""
        return self.inspect_html(page.content(), url or str(getattr(page, "url", "")), form_id)

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
        if first.name == "select":
            for option in first.find_all("option"):
                label = option.get_text(" ", strip=True) or str(option.get("value", ""))
                value = _css_escape(str(option.get("value", "")))
                option_locators[label] = f'{locator} option[value="{value}"]'
        else:
            for option, element in zip(options, group):
                option_locators[option] = _base_locator(element)
        return DOMFieldBinding(field_key, locator, field_type, option_locators)
