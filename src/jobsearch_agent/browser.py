"""Offline Browser Dry Run executor and optional Playwright session wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from .execution import ExecutionPlan, validate_execution_context
from .models import ApplicationContext, ValidationResult


class BrowserSessionError(RuntimeError):
    pass


def validate_navigation_url(url: str) -> ValidationResult:
    """Reject local, private and non-web URLs before Playwright sees them."""
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["only http and https URLs are allowed"])
    if not parsed.hostname or parsed.username or parsed.password:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["URL must contain a public hostname without credentials"])
    hostname = parsed.hostname.rstrip(".").casefold()
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

    def execute(self, context: ApplicationContext, plan: ExecutionPlan) -> BrowserExecutionResult:
        raise NotImplementedError


class DryRunBrowserExecutor(BrowserExecutor):
    """Record browser operations without opening a browser or sending data."""

    def execute(self, context: ApplicationContext, plan: ExecutionPlan) -> BrowserExecutionResult:
        validation = validate_execution_context(context, plan)
        if not validation.valid:
            raise BrowserSessionError("refusing invalid execution plan: " + "; ".join(validation.errors))
        operations = []
        for action in plan.actions:
            if action.action_type == "fill":
                operations.append({"operation": "fill", "field_key": action.field_key, "value": action.value, "step": action.step})
            elif action.action_type == "upload":
                operations.append({"operation": "upload", "field_key": action.field_key, "attachment_path": action.attachment_path, "step": action.step})
        return BrowserExecutionResult(plan.application_id, operations, stopped_before_submit=True)


class PlaywrightSessionManager:
    """Small optional session wrapper; intentionally has no submit operation."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    @staticmethod
    def _guard_route(route: Any) -> None:  # pragma: no cover - exercised with Playwright installed
        validation = validate_navigation_url(route.request.url)
        if validation.valid:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def start(self) -> None:
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
        validation = validate_navigation_url(url)
        if not validation.valid:
            raise BrowserSessionError("refusing unsafe navigation: " + "; ".join(validation.errors))
        self.page.goto(url, wait_until="domcontentloaded")
        final_validation = validate_navigation_url(self.page.url)
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
