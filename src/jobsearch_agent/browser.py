"""Offline Browser Dry Run executor and optional Playwright session wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
import socket
from typing import Any
from urllib.parse import urlparse

from .execution import ExecutionPlan, validate_execution_context
from .inspector import FormBindings, fingerprint_html
from .models import ApplicationContext, ValidationResult


class BrowserSessionError(RuntimeError):
    pass


@dataclass
class NetworkRequestEvent:
    url: str
    method: str
    resource_type: str
    allowed: bool
    reason: str = ""


class NetworkWriteGuard:
    """Deny all browser write requests during a dry-run session."""

    READ_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, allowed_hosts: set[str]):
        self.allowed_hosts = allowed_hosts
        self.events: list[NetworkRequestEvent] = []

    @property
    def blocked_writes(self) -> list[NetworkRequestEvent]:
        return [event for event in self.events if not event.allowed and (event.method not in self.READ_METHODS or event.resource_type == "websocket")]

    def inspect(self, request: Any) -> bool:
        method = str(getattr(request, "method", "GET")).upper()
        resource_type = str(getattr(request, "resource_type", ""))
        url = str(getattr(request, "url", ""))
        if resource_type == "websocket":
            self.events.append(NetworkRequestEvent(url, method, resource_type, False, "websocket blocked in dry-run"))
            return False
        if method not in self.READ_METHODS:
            self.events.append(NetworkRequestEvent(url, method, resource_type, False, "write method blocked in dry-run"))
            return False
        self.events.append(NetworkRequestEvent(url, method, resource_type, True))
        return True


def validate_navigation_url(url: str, allowed_hosts: set[str] | None = None, *, resource: bool = False) -> ValidationResult:
    """Reject local, private and non-web URLs before Playwright sees them."""
    parsed = urlparse(url)
    if resource and parsed.scheme.casefold() in {"data", "blob"}:
        return ValidationResult(True, "OK")
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["only http and https URLs are allowed"])
    if not parsed.hostname or parsed.username or parsed.password:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["URL must contain a public hostname without credentials"])
    hostname = parsed.hostname.rstrip(".").casefold()
    normalized_hosts = {item.casefold().rstrip(".") for item in (allowed_hosts or set())}
    exact_hosts = {item for item in normalized_hosts if not item.startswith("*.")}
    wildcard_hosts = {item[2:] for item in normalized_hosts if item.startswith("*.")}
    if allowed_hosts and hostname not in exact_hosts and not any(hostname != suffix and hostname.endswith("." + suffix) for suffix in wildcard_hosts):
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["hostname is not in the approved navigation policy"])
    try:
        port = parsed.port
    except ValueError:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["invalid URL port"])
    if port is not None and port not in {80, 443}:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["only ports 80 and 443 are allowed"])
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["local hostnames are not allowed"])
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)]
        except (OSError, ValueError):
            return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["hostname could not be resolved safely"])
    blocked = [address for address in addresses if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified or address.is_multicast]
    if blocked:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["private, loopback, link-local or reserved addresses are not allowed"])
    return ValidationResult(True, "OK")


@dataclass
class BrowserExecutionResult:
    application_id: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    stopped_before_submit: bool = True
    status: str = "COMPLETED"
    initial_fingerprint: str = ""
    current_fingerprint: str = ""
    submission_attempted: bool = False
    network_writes_allowed: bool = False


@dataclass
class DryRunAuditReport:
    application_id: str
    result: str
    executed_actions: int
    pending_actions: int
    initial_fingerprint: str
    current_fingerprint: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    submission_attempted: bool = False
    network_writes_allowed: bool = False


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_dry_run_report(path: str | Path, report: DryRunAuditReport) -> None:
    _write_json(Path(path), {key: value for key, value in report.__dict__.items()})


class BrowserExecutor:
    """Minimal executor contract; intentionally exposes no submit operation."""

    def execute(self, context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings, current_html: str | None = None) -> BrowserExecutionResult:
        raise NotImplementedError


class DryRunBrowserExecutor(BrowserExecutor):
    """Record browser operations without opening a browser or sending data."""

    def execute(self, context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings, current_html: str | None = None) -> BrowserExecutionResult:
        validation = validate_execution_context(context, plan, bindings, current_html)
        if not validation.valid:
            raise BrowserSessionError("refusing invalid execution plan: " + "; ".join(validation.errors))
        operations = []
        for action in plan.actions:
            if action.action_type == "fill":
                operations.append({"operation": "fill", "field_key": action.field_key, "value": action.value, "step": action.step})
            elif action.action_type == "upload":
                operations.append({"operation": "upload", "field_key": action.field_key, "attachment_path": action.attachment_path, "step": action.step})
        return BrowserExecutionResult(plan.application_id, operations, stopped_before_submit=True)


class PlaywrightFormFiller:
    """Fill only validated controls; this class intentionally has no submit method."""

    def fill(self, page: Any, context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings, audit_dir: str | Path | None = None) -> BrowserExecutionResult:
        if not page.evaluate("() => window.__jobsearchDryRun === true"):
            raise BrowserSessionError("Playwright page is not attached to a dry-run guarded session")
        current_html = page.content()
        validation = validate_execution_context(context, plan, bindings, current_html, str(getattr(page, "url", "")))
        if not validation.valid:
            raise BrowserSessionError("refusing stale or invalid form: " + "; ".join(validation.errors))
        fields = {field.key: field for field in context.form.fields}
        operations: list[dict[str, Any]] = []
        audit_path = Path(audit_dir) if audit_dir else None
        if audit_path:
            audit_path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(audit_path / "screenshot-before.png"))
            _write_json(audit_path / "form.json", {"application_id": context.application_id, "fingerprint": plan.form_fingerprint, "fields": [{"key": field.key, "label": field.label, "field_type": field.field_type, "required": field.required, "options": field.options} for field in context.form.fields]})
            _write_json(audit_path / "bindings.json", {"form_id": bindings.form_id, "root_locator": bindings.root_locator, "fields": [binding.__dict__ for binding in bindings.fields]})
            _write_json(audit_path / "execution-plan.json", {"application_id": plan.application_id, "provider": plan.provider, "form_fingerprint": plan.form_fingerprint, "actions": [{"action_type": action.action_type, "field_key": action.field_key, "sha256": action.sha256} for action in plan.actions]})
        initial_fingerprint = plan.form_fingerprint
        for index, action in enumerate(plan.actions):
            field = fields[action.field_key]
            binding = bindings.for_field(action.field_key)
            if binding is None:
                raise BrowserSessionError(f"missing binding: {action.field_key}")
            locator = page.locator(binding.locator)
            if locator.count() != 1:
                raise BrowserSessionError(f"locator is not unique: {action.field_key}")
            field_type = field.field_type.casefold().strip()
            if action.action_type == "upload":
                locator.set_input_files(action.attachment_path)
                operations.append({"operation": "upload", "field_key": action.field_key})
            elif action.action_type == "fill":
                self._fill_value(page, locator, binding, field, action.value)
                operations.append({"operation": "fill", "field_key": action.field_key})
            else:  # defensive; validate_execution_context already rejects it
                raise BrowserSessionError(f"unsupported dry-run action: {action.action_type}")
            page.wait_for_timeout(0)
            current_html = page.content()
            current_validation = validate_execution_context(context, plan, bindings, current_html, str(getattr(page, "url", "")))
            if not current_validation.valid:
                try:
                    current_fingerprint = fingerprint_html(context.form, bindings, current_html, str(getattr(page, "url", "")))
                except Exception:
                    current_fingerprint = ""
                report = DryRunAuditReport(plan.application_id, "FORM_CHANGED", len(operations), len(plan.actions) - index - 1, initial_fingerprint, current_fingerprint, operations)
                if audit_path:
                    page.screenshot(path=str(audit_path / "screenshot-after.png"))
                    _write_json(audit_path / "operations.json", operations)
                    write_dry_run_report(audit_path / "dry-run-report.json", report)
                return BrowserExecutionResult(plan.application_id, operations, True, "FORM_CHANGED", initial_fingerprint, current_fingerprint, False, False)
        result = BrowserExecutionResult(plan.application_id, operations, True, "COMPLETED", initial_fingerprint, initial_fingerprint, False, False)
        if audit_path:
            page.screenshot(path=str(audit_path / "screenshot-after.png"))
            _write_json(audit_path / "operations.json", operations)
            write_dry_run_report(audit_path / "dry-run-report.json", DryRunAuditReport(plan.application_id, result.status, len(operations), 0, initial_fingerprint, result.current_fingerprint, operations))
        return result

    @staticmethod
    def _fill_value(page: Any, locator: Any, binding: FormBindings | Any, field: Any, value: Any) -> None:
        field_type = field.field_type.casefold().strip()
        if field_type == "select":
            option_value = binding.option_values.get(str(value))
            if option_value is None:
                raise BrowserSessionError(f"select option is not bound: {field.key}")
            locator.select_option(option_value)
        elif field_type == "radio":
            option_locator = binding.option_locators.get(str(value))
            if not option_locator:
                raise BrowserSessionError(f"radio option is not bound: {field.key}")
            page.locator(option_locator).check()
        elif field_type == "checkbox":
            if field.semantic_type == "checkbox_boolean":
                normalized = str(value).casefold()
                if normalized in {"true", "yes", "1", "on", "checked"}:
                    locator.check()
                else:
                    locator.uncheck()
            else:
                selected = value if isinstance(value, list) else str(value).split(",")
                for label, option_locator in binding.option_locators.items():
                    option = page.locator(option_locator)
                    if label in selected:
                        option.check()
                    else:
                        option.uncheck()
        else:
            locator.fill(str(value))


class PlaywrightSessionManager:
    """Small optional session wrapper; intentionally has no submit operation."""

    def __init__(self, headless: bool = True, allowed_hosts: set[str] | None = None):
        self.headless = headless
        self.allowed_hosts = allowed_hosts
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.network_guard: NetworkWriteGuard | None = None

    _DRY_RUN_INIT_SCRIPT = """
        (() => {
          window.__jobsearchDryRun = true;
          const blocked = () => { throw new Error('blocked by jobsearch-agent dry-run'); };
          if (window.HTMLFormElement) {
            window.HTMLFormElement.prototype.submit = blocked;
            window.HTMLFormElement.prototype.requestSubmit = blocked;
          }
          document.addEventListener('submit', event => { event.preventDefault(); event.stopImmediatePropagation(); }, true);
          if (navigator.sendBeacon) navigator.sendBeacon = () => false;
          if (window.WebSocket) window.WebSocket = function() { throw new Error('websocket blocked by jobsearch-agent dry-run'); };
        })();
    """

    def _guard_route(self, route: Any) -> None:  # pragma: no cover - exercised with Playwright installed
        request_url = route.request.url
        parsed = urlparse(request_url)
        if parsed.scheme.casefold() in {"data", "blob"}:
            frame_url = getattr(route.request.frame, "url", "")
            frame_validation = validate_navigation_url(frame_url, self.allowed_hosts, resource=False)
            if frame_validation.valid:
                route.continue_()
            else:
                route.abort("blockedbyclient")
            return
        validation = validate_navigation_url(request_url, self.allowed_hosts, resource=True)
        if not validation.valid:
            if self.network_guard:
                self.network_guard.events.append(NetworkRequestEvent(request_url, str(getattr(route.request, "method", "GET")), str(getattr(route.request, "resource_type", "")), False, "; ".join(validation.errors)))
            route.abort("blockedbyclient")
            return
        network_allowed = self.network_guard.inspect(route.request) if self.network_guard else False
        if network_allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def start(self) -> None:
        if not self.allowed_hosts:
            raise BrowserSessionError("Playwright session requires an explicit allowed_hosts policy")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BrowserSessionError("Playwright is not installed") from exc
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)
        self.context = self.browser.new_context(service_workers="block")
        self.network_guard = NetworkWriteGuard(self.allowed_hosts)
        self.context.add_init_script(self._DRY_RUN_INIT_SCRIPT)
        self.context.route("**/*", self._guard_route)
        self.page = self.context.new_page()

    def open(self, url: str) -> None:
        if self.page is None:
            raise BrowserSessionError("Playwright session is not started")
        validation = validate_navigation_url(url, self.allowed_hosts)
        if not validation.valid:
            raise BrowserSessionError("refusing unsafe navigation: " + "; ".join(validation.errors))
        self.page.goto(url, wait_until="domcontentloaded")
        final_validation = validate_navigation_url(self.page.url, self.allowed_hosts)
        if not final_validation.valid:
            raise BrowserSessionError("navigation redirected to an unsafe URL")

    def close(self) -> None:
        if self.browser is not None:
            if self.context is not None:
                self.context.close()
            self.browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self.browser = None
        self.context = None
        self.page = None
        self._playwright = None
        self.network_guard = None
