"""Offline Browser Dry Run executor and optional Playwright session wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from .execution import ExecutionPlan, validate_execution_context
from .inspector import FormBindings
from .models import ApplicationContext, ValidationResult


class BrowserSessionError(RuntimeError):
    pass


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
    if allowed_hosts and hostname not in {item.casefold().rstrip(".") for item in allowed_hosts} and not any(hostname.endswith("." + item.casefold().rstrip(".")) for item in allowed_hosts):
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

    def fill(self, page: Any, context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings) -> BrowserExecutionResult:
        current_html = page.content()
        validation = validate_execution_context(context, plan, bindings, current_html, str(getattr(page, "url", "")))
        if not validation.valid:
            raise BrowserSessionError("refusing stale or invalid form: " + "; ".join(validation.errors))
        fields = {field.key: field for field in context.form.fields}
        operations: list[dict[str, Any]] = []
        for action in plan.actions:
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
        return BrowserExecutionResult(plan.application_id, operations, stopped_before_submit=True)

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
        if validation.valid:
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
        self.context = self.browser.new_context()
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
