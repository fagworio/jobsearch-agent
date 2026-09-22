"""Offline Browser Dry Run executor and optional Playwright session wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .execution import ExecutionPlan, validate_execution_plan


class BrowserSessionError(RuntimeError):
    pass


@dataclass
class BrowserExecutionResult:
    application_id: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    stopped_before_submit: bool = True


class BrowserExecutor:
    """Minimal executor contract; intentionally exposes no submit operation."""

    def execute(self, plan: ExecutionPlan) -> BrowserExecutionResult:
        raise NotImplementedError


class DryRunBrowserExecutor(BrowserExecutor):
    """Record browser operations without opening a browser or sending data."""

    def execute(self, plan: ExecutionPlan) -> BrowserExecutionResult:
        validation = validate_execution_plan(plan)
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
        self.page = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BrowserSessionError("Playwright is not installed") from exc
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)
        self.page = self.browser.new_page()

    def open(self, url: str) -> None:
        if self.page is None:
            raise BrowserSessionError("Playwright session is not started")
        self.page.goto(url, wait_until="domcontentloaded")

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self.browser = None
        self.page = None
        self._playwright = None
