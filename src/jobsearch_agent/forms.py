"""Validation of ATS-neutral application forms.

The validator is deliberately independent of a browser.  It is the single
place that decides whether a field value is structurally safe to execute.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import zipfile

from .models import ApplicationField, ApplicationForm, ValidationResult


SUPPORTED_FIELD_TYPES = {
    "text", "textarea", "email", "tel", "url", "select", "radio", "checkbox", "date", "file",
}
TRUE_VALUES = {"true", "yes", "1", "on", "checked"}
FALSE_VALUES = {"false", "no", "0", "off", "unchecked"}


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip().casefold())


def _effective_value(field: ApplicationField) -> Any:
    if field.value not in (None, ""):
        return field.value
    return field.answer.answer if field.answer else ""


def effective_attachment_path(field: ApplicationField) -> str:
    """Return the only accepted source of a file upload path."""
    return str(field.attachment_path or "")


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _checkbox_kind(field: ApplicationField) -> str:
    if field.semantic_type in {"checkbox_boolean", "checkbox_multi"}:
        return field.semantic_type
    if field.multiple or len(field.options) > 1:
        return "checkbox_multi"
    return "checkbox_boolean"


def _validate_options(field: ApplicationField, value: Any) -> str:
    if not field.options or _is_blank(value):
        return ""
    if field.field_type == "checkbox" and _checkbox_kind(field) == "checkbox_boolean" and isinstance(value, bool):
        value = "Yes" if value else "No"
    options = {_normalized(option) for option in field.options}
    if field.field_type == "checkbox" and _checkbox_kind(field) == "checkbox_multi":
        selected = value if isinstance(value, list) else str(value).split(",")
        invalid = [item for item in selected if _normalized(item) not in options]
        if invalid:
            return f"value is not among options: {', '.join(map(str, invalid))}"
        return ""
    if _normalized(value) not in options:
        return "value is not among options"
    return ""


def _validate_checkbox(field: ApplicationField, value: Any) -> list[str]:
    if _checkbox_kind(field) == "checkbox_multi":
        selected = value if isinstance(value, list) else ([] if _is_blank(value) else str(value).split(","))
        if field.required and not selected:
            return ["at least one checkbox option is required"]
        return []
    normalized = _normalized(value)
    if normalized in TRUE_VALUES:
        return []
    if normalized in FALSE_VALUES:
        return ["required checkbox must be checked"] if field.required else []
    return ["checkbox value must be true or false"]


def detect_file_mime(path: Path) -> str:
    """Detect the small set of document types accepted by the pipeline."""
    try:
        header = path.read_bytes()[:16]
    except OSError:
        return "application/octet-stream"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" in archive.namelist():
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except (OSError, zipfile.BadZipFile):
            pass
        return "application/zip"
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "application/octet-stream"
    return "text/plain"


def _validate_file(field: ApplicationField, artifact_root: str) -> list[str]:
    path_value = effective_attachment_path(field)
    if field.required and not path_value:
        return ["required file artifact is missing"]
    if not path_value:
        return []
    if not artifact_root:
        return ["controlled artifact root is not configured"]
    root = Path(artifact_root).expanduser().resolve()
    path = Path(path_value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return ["file artifact is outside the controlled artifact root"]
    if not path.is_file():
        return ["file artifact does not exist"]
    actual_mime = detect_file_mime(path)
    expected_by_suffix = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
    }
    expected_mime = expected_by_suffix.get(path.suffix.casefold())
    if expected_mime and actual_mime != expected_mime:
        return [f"file content does not match {path.suffix.casefold() or 'unknown'}"]
    if field.accepted_types:
        suffix = path.suffix.casefold()
        accepted = {_normalized(item).lstrip(".") for item in field.accepted_types}
        accepted_suffixes = {"." + item for item in accepted if "/" not in item}
        accepted_mimes = {item for item in accepted if "/" in item}
        if suffix not in accepted_suffixes and _normalized(suffix).lstrip(".") not in accepted and actual_mime not in accepted_mimes:
            return [f"file type {suffix or 'unknown'} is not accepted"]
    return []


def validate_application_field(field: ApplicationField, artifact_root: str = "") -> ValidationResult:
    field_type = field.field_type.casefold().strip()
    if field_type not in SUPPORTED_FIELD_TYPES:
        return ValidationResult(False, "UNSUPPORTED_FORM_FIELD", [f"unsupported field type: {field.field_type}"], details={"field_key": field.key})

    value = _effective_value(field)
    errors: list[str] = []
    if field.disabled and (field.required or not _is_blank(value) or field_type == "file"):
        errors.append("field is disabled")
    elif field_type == "file":
        errors.extend(_validate_file(field, artifact_root))
    elif field_type == "checkbox":
        if field.required and _is_blank(value):
            errors.append("required checkbox has no value")
        else:
            errors.extend(_validate_checkbox(field, value))
            option_error = _validate_options(field, value)
            if option_error:
                errors.append(option_error)
    else:
        if field.required and _is_blank(value):
            errors.append("required field has no value")
        option_error = _validate_options(field, value)
        if option_error:
            errors.append(option_error)

    return ValidationResult(
        not errors,
        "OK" if not errors else "INVALID_FORM_FIELD",
        errors,
        details={"field_key": field.key, "field_type": field_type},
    )


def validate_application_form(form: ApplicationForm) -> ValidationResult:
    field_errors: dict[str, list[str]] = {}
    unsupported: list[str] = []
    for field in form.fields:
        result = validate_application_field(field, form.artifact_root)
        if not result.valid:
            field_errors[field.key] = result.errors
            if result.code == "UNSUPPORTED_FORM_FIELD":
                unsupported.append(field.key)
    if unsupported:
        code = "UNSUPPORTED_FORM"
    elif field_errors:
        code = "INVALID_FORM"
    else:
        code = "OK"
    return ValidationResult(
        not field_errors,
        code,
        [f"{key}: {error}" for key, errors in field_errors.items() for error in errors],
        details={"field_errors": field_errors, "unsupported_fields": unsupported},
    )
