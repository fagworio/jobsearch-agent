"""Adapter do snapshot de resultados produzido pela extensão."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import DiscoveryFilter, DiscoveryFilterOption, DiscoveryFilters, DiscoveryJob, DiscoveryResults


class DiscoveryInspectionError(ValueError):
    """Snapshot de resultados ausente, inconsistente ou de outro provider."""


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise DiscoveryInspectionError(f"{name} must be a string")
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise DiscoveryInspectionError(f"{name} must be boolean")
    return value


def _job(value: object, index: int) -> DiscoveryJob:
    if not isinstance(value, Mapping):
        raise DiscoveryInspectionError(f"jobs[{index}] must be an object")
    required_strings = ("provider", "job_id", "title", "company", "href", "work_type", "location", "posted", "status")
    strings = {key: _string(value.get(key), f"jobs[{index}].{key}") for key in required_strings}
    salary = value.get("salary")
    if salary is not None and not isinstance(salary, str):
        raise DiscoveryInspectionError(f"jobs[{index}].salary must be string or null")
    description = value.get("description", "")
    if not isinstance(description, str):
        raise DiscoveryInspectionError(f"jobs[{index}].description must be a string")
    return DiscoveryJob(
        **strings,
        remote=_bool(value.get("remote"), f"jobs[{index}].remote"),
        salary=salary,
        applied=_bool(value.get("applied"), f"jobs[{index}].applied"),
        viewed=_bool(value.get("viewed"), f"jobs[{index}].viewed"),
        description=description,
    )


class GreenhouseDiscoveryAdapter:
    provider = "greenhouse"

    def inspect(self, snapshot: Mapping[str, Any]) -> DiscoveryResults:
        if snapshot.get("provider") != self.provider:
            raise DiscoveryInspectionError("snapshot provider is not greenhouse")
        page_type = _string(snapshot.get("page_type"), "page_type")
        if page_type != "search":
            raise DiscoveryInspectionError(f"unsupported Greenhouse discovery page type: {page_type}")
        if snapshot.get("ready") is not True:
            raise DiscoveryInspectionError("Greenhouse search is not ready")
        raw_work_type = snapshot.get("work_type", [])
        if not isinstance(raw_work_type, list) or not all(isinstance(item, str) for item in raw_work_type):
            raise DiscoveryInspectionError("work_type must be a string list")
        raw_jobs = snapshot.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise DiscoveryInspectionError("jobs must be a list")
        return DiscoveryResults(
            provider=self.provider,
            page_type=page_type,
            surface=_string(snapshot.get("surface"), "surface"),
            url=_string(snapshot.get("url"), "url"),
            title=_string(snapshot.get("title"), "title"),
            ready=True,
            query=_string(snapshot.get("query"), "query"),
            work_type=tuple(raw_work_type),
            jobs=tuple(_job(item, index) for index, item in enumerate(raw_jobs)),
        )

    def inspect_filters(self, snapshot: Mapping[str, Any]) -> DiscoveryFilters:
        if snapshot.get("provider") != self.provider:
            raise DiscoveryInspectionError("snapshot provider is not greenhouse")
        page_type = _string(snapshot.get("page_type"), "page_type")
        if page_type != "search":
            raise DiscoveryInspectionError(f"unsupported Greenhouse filter page type: {page_type}")
        if snapshot.get("ready") is not True:
            raise DiscoveryInspectionError("Greenhouse search is not ready")

        raw_parameters = snapshot.get("parameters", {})
        if not isinstance(raw_parameters, Mapping):
            raise DiscoveryInspectionError("parameters must be an object")
        parameters: dict[str, tuple[str, ...]] = {}
        for key, values in raw_parameters.items():
            if not isinstance(key, str) or not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise DiscoveryInspectionError("parameters must map strings to string lists")
            parameters[key] = tuple(values)

        raw_filters = snapshot.get("filters", [])
        if not isinstance(raw_filters, list):
            raise DiscoveryInspectionError("filters must be a list")
        filters: list[DiscoveryFilter] = []
        for index, value in enumerate(raw_filters):
            if not isinstance(value, Mapping):
                raise DiscoveryInspectionError(f"filters[{index}] must be an object")
            key = _string(value.get("key"), f"filters[{index}].key")
            label = _string(value.get("label"), f"filters[{index}].label")
            control = _string(value.get("control"), f"filters[{index}].control")
            parameter = _string(value.get("parameter"), f"filters[{index}].parameter")
            selected = value.get("selected", [])
            if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
                raise DiscoveryInspectionError(f"filters[{index}].selected must be a string list")
            raw_options = value.get("options", [])
            if not isinstance(raw_options, list):
                raise DiscoveryInspectionError(f"filters[{index}].options must be a list")
            options: list[DiscoveryFilterOption] = []
            for option_index, raw_option in enumerate(raw_options):
                if not isinstance(raw_option, Mapping):
                    raise DiscoveryInspectionError(f"filters[{index}].options[{option_index}] must be an object")
                options.append(DiscoveryFilterOption(
                    label=_string(raw_option.get("label"), f"filters[{index}].options[{option_index}].label"),
                    value=_string(raw_option.get("value"), f"filters[{index}].options[{option_index}].value"),
                    selected=_bool(raw_option.get("selected"), f"filters[{index}].options[{option_index}].selected"),
                ))
            filters.append(DiscoveryFilter(key, label, control, parameter, tuple(selected), tuple(options)))

        return DiscoveryFilters(
            provider=self.provider,
            page_type=page_type,
            surface=_string(snapshot.get("surface"), "surface"),
            url=_string(snapshot.get("url"), "url"),
            title=_string(snapshot.get("title"), "title"),
            ready=True,
            query=_string(snapshot.get("query"), "query"),
            parameters=parameters,
            filters=tuple(filters),
        )
