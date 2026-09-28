"""Semântica Greenhouse sobre o snapshot neutro produzido pela extensão."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import Field, Form
from .base import ATSInspectionError, NeutralField, NeutralForm


def _string(value: object, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ATSInspectionError(f"field {name} must be a string")
    return value


def _neutral_field(value: object, index: int) -> NeutralField:
    if not isinstance(value, Mapping):
        raise ATSInspectionError(f"field {index} must be an object")
    field_id = _string(value.get("id"), f"fields[{index}].id")
    field_type = _string(value.get("type"), f"fields[{index}].type")
    label = _string(value.get("label"), f"fields[{index}].label", allow_empty=True)
    required = value.get("required")
    if not isinstance(required, bool):
        raise ATSInspectionError(f"fields[{index}].required must be boolean")
    raw_options = value.get("options", [])
    if not isinstance(raw_options, list) or not all(isinstance(item, str) for item in raw_options):
        raise ATSInspectionError(f"fields[{index}].options must be a string list")
    current = value.get("value", "")
    if not isinstance(current, str):
        raise ATSInspectionError(f"fields[{index}].value must be a string")
    return NeutralField(field_id, field_type, label, required, tuple(raw_options), current)


class GreenhouseAdapter:
    provider = "greenhouse"

    def inspect(self, snapshot: Mapping[str, Any]) -> NeutralForm:
        if snapshot.get("provider") != self.provider:
            raise ATSInspectionError("snapshot provider is not greenhouse")
        page_type = _string(snapshot.get("page_type"), "page_type")
        if page_type != "application":
            raise ATSInspectionError(f"unsupported Greenhouse page type: {page_type}")
        fields = tuple(_neutral_field(item, index) for index, item in enumerate(snapshot.get("fields", [])))
        if not fields:
            raise ATSInspectionError("Greenhouse application has no fields")
        return NeutralForm(
            provider=self.provider,
            page_type=page_type,
            url=_string(snapshot.get("url"), "url"),
            title=_string(snapshot.get("title"), "title", allow_empty=True),
            ready=snapshot.get("ready") is True,
            fields=fields,
        )

    def to_form(self, snapshot: Mapping[str, Any]) -> Form:
        neutral = self.inspect(snapshot)
        return Form(
            tuple(
                Field(
                    key=item.id,
                    prompt=item.label or item.id,
                    options=item.options,
                    required=item.required,
                    kind=item.type,
                )
                for item in neutral.fields
            )
        )
