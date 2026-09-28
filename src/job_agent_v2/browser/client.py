"""Clientes controlados da V2.

``NativeMessagingClient`` só transporta mensagens do contrato fechado. O
loader Playwright continua disponível exclusivamente para testes controlados e
medição, não como runtime de produção da candidatura.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import subprocess
import sys
from typing import BinaryIO

from .native_host import read_frame, write_frame
from .protocol import Command, Request, Response


class NativeMessagingClient:
    """Cliente de transporte para um host Native Messaging local."""

    def __init__(self, command: Sequence[str] | None = None) -> None:
        argv = tuple(command or (sys.executable, "-m", "job_agent_v2.browser.native_host"))
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    @property
    def _stdin(self) -> BinaryIO:
        if self._process.stdin is None:
            raise RuntimeError("native host stdin is unavailable")
        return self._process.stdin

    @property
    def _stdout(self) -> BinaryIO:
        if self._process.stdout is None:
            raise RuntimeError("native host stdout is unavailable")
        return self._process.stdout

    def request(self, request: Request) -> Response:
        write_frame(self._stdin, request.to_json())
        raw = read_frame(self._stdout)
        if raw is None:
            raise RuntimeError("native host closed before responding")
        return Response.from_object(json.loads(raw.decode("utf-8")))

    def ping(self) -> Response:
        return self.request(Request.create("client-ping", Command.PING))

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.terminate()
        self._process.wait(timeout=5)


def open_page_html(url: str, *, timeout_ms: float = 45_000) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_context().new_page()
            page.goto(url, wait_until="load", timeout=timeout_ms)
            return page.content()
        finally:
            browser.close()
