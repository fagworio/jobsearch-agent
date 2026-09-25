"""Sessao de browser do V2-001A: abrir a pagina e obter o DOM. Nada mais.

Sem upload, sem submit, sem write guard, sem relay: nao existe escrita neste
incremento, entao nao se constroi infraestrutura de escrita. Playwright entra
por import tardio para que o modulo offline nao dependa dele.
"""

from __future__ import annotations


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
