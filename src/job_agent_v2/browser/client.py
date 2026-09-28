"""Cliente browser mínimo da V2.

Este módulo só abre uma página e devolve o DOM. Preenchimento, autorização e
submissão ficam em contratos separados e serão substituídos pelo cliente de
extensão Chrome nas fases seguintes.
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
