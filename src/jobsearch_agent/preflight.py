"""Public, read-only browser preflight diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .ats import adapter_for
from .browser import DOMStabilityGuard, BrowserSessionError, NetworkRequestEvent, PlaywrightSessionManager
from .inspector import InspectionError


@dataclass
class PreflightResult:
    status: str
    requested_url: str
    final_url: str = ""
    application_frame_url: str = ""
    provider: str = ""
    adapter_confidence: float = 0.0
    application_root: str = ""
    field_count: int = 0
    field_keys: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    unsupported_features: list[str] = field(default_factory=list)
    capability_issues: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked_get_origins: list[str] = field(default_factory=list)
    blocked_get_count: int = 0
    blocked_write_count: int = 0
    blocked_websocket_count: int = 0
    network_guard_active: bool = False
    network_writes_allowed: bool = False
    submission_attempted: bool = False
    artifact_path: str = ""
    error: str = ""


def _network_diagnostics(events: list[NetworkRequestEvent]) -> tuple[list[str], int, int, int]:
    blocked_gets = [event for event in events if not event.allowed and event.method in {"GET", "HEAD", "OPTIONS"} and event.resource_type != "websocket"]
    origins = sorted({event.origin for event in blocked_gets if event.origin})
    blocked_writes = [event for event in events if not event.allowed and (event.method not in {"GET", "HEAD", "OPTIONS"} or event.resource_type == "websocket")]
    blocked_websockets = [event for event in blocked_writes if event.resource_type == "websocket"]
    return origins, len(blocked_gets), len(blocked_writes), len(blocked_websockets)


def _write_result(result: PreflightResult, artifact_root: str | Path | None) -> None:
    if not artifact_root:
        return
    digest = hashlib.sha256(result.requested_url.encode("utf-8")).hexdigest()[:16]
    directory = Path(artifact_root).expanduser().resolve() / "preflight" / digest
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "preflight.json"
    result.artifact_path = str(path)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _inspect_application_frames(page: Any, default_url: str) -> tuple[Any, Any, str, str]:
    inspection = None
    detected_adapter = None
    provider_seen = ""
    last_error = ""
    frames = list(getattr(page, "frames", []) or [page])
    for frame in frames:
        frame_url = str(getattr(frame, "url", default_url) or default_url)
        try:
            html = frame.content()
        except Exception:
            continue
        detected = adapter_for(frame_url, html)
        if detected is None:
            continue
        provider_seen = detected.provider
        try:
            inspection = detected.inspect(html, frame_url)
        except InspectionError as exc:
            last_error = str(exc)
            continue
        detected_adapter = detected
        return inspection, detected_adapter, frame_url, provider_seen
    return inspection, detected_adapter, "", provider_seen or last_error


def _click_safe_apply_button(page: Any) -> bool:
    """Open the application surface only through a non-submit Apply button."""
    button = page.locator('button[aria-label="Apply"]')
    if button.count() != 1:
        return False
    button_type = (button.get_attribute("type") or "submit").casefold()
    if button_type != "button":
        return False
    button.click()
    return True


def run_preflight(url: str, artifact_root: str | Path | None = None, *, headless: bool = True) -> PreflightResult:
    """Open and inspect a public page without filling controls or submitting."""
    from .ats import GreenhouseAdapter
    from .browser import validate_navigation_url

    adapter = GreenhouseAdapter()
    allowed_hosts = adapter.allowed_hosts(url)
    result = PreflightResult("ERROR", url, allowed_hosts=sorted(allowed_hosts))
    manager: PlaywrightSessionManager | None = None
    settling_issue = ""
    try:
        navigation = validate_navigation_url(url, allowed_hosts)
        if not navigation.valid:
            result.error = "; ".join(navigation.errors)
            return result
        manager = PlaywrightSessionManager(headless=headless, allowed_hosts=allowed_hosts)
        manager.start()
        result.network_guard_active = manager.guarded and manager.network_guard is not None
        manager.open(url)
        final_url = str(manager.page.url)
        result.final_url = final_url
        final_validation = validate_navigation_url(final_url, allowed_hosts)
        if not final_validation.valid:
            raise BrowserSessionError("final URL rejected: " + "; ".join(final_validation.errors))
        try:
            DOMStabilityGuard().wait(manager.page, lambda: manager.network_guard.pending_read_count if manager.network_guard else 0)
        except BrowserSessionError as exc:
            settling_issue = str(exc)
            result.warnings.append(settling_issue)
        inspection, detected, frame_url, provider_or_error = _inspect_application_frames(manager.page, final_url)
        if inspection is None and _click_safe_apply_button(manager.page):
            try:
                DOMStabilityGuard().wait(manager.page, lambda: manager.network_guard.pending_read_count if manager.network_guard else 0)
            except BrowserSessionError as exc:
                settling_issue = str(exc)
                result.warnings.append(settling_issue)
            final_url = str(manager.page.url)
            result.final_url = final_url
            final_validation = validate_navigation_url(final_url, allowed_hosts)
            if not final_validation.valid:
                raise BrowserSessionError("final URL rejected after Apply: " + "; ".join(final_validation.errors))
            inspection, detected, frame_url, provider_or_error = _inspect_application_frames(manager.page, final_url)
        if inspection is None:
            result.provider = provider_or_error if provider_or_error in {"greenhouse", "lever", "ashby"} else ""
            result.status = "UNSUPPORTED_PROVIDER" if not result.provider else "ERROR"
            result.error = provider_or_error if result.provider == "" else "no supported ATS application form matched the public page"
            return result
        result.provider = detected.provider
        result.adapter_confidence = inspection.confidence
        result.application_frame_url = frame_url
        result.application_root = inspection.bindings.root_locator
        result.field_count = len(inspection.form.fields)
        result.field_keys = [item.key for item in inspection.form.fields]
        result.unsupported_features = list(inspection.unsupported_features)
        result.capability_issues = [asdict(issue) for issue in inspection.form.capability_issues]
        result.warnings.extend(inspection.warnings)
        if any(issue["severity"] == "blocker" for issue in result.capability_issues):
            result.status = "UNSUPPORTED_FORM"
        else:
            result.status = "DOM_UNSTABLE" if settling_issue else "READY"
    except (BrowserSessionError, InspectionError, OSError, ValueError) as exc:
        result.error = str(exc)
    finally:
        if manager is not None:
            events = list(manager.network_guard.events) if manager.network_guard else []
            result.blocked_get_origins, result.blocked_get_count, result.blocked_write_count, result.blocked_websocket_count = _network_diagnostics(events)
            result.network_guard_active = result.network_guard_active or manager.guarded
            manager.close()
        _write_result(result, artifact_root)
    return result
