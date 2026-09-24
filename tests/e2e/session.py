"""Sessão de teste para o ATS controlado em loopback (JSA-E2E-001).

A política de hosts do produto é fechada por provider (`greenhouse.io`, …), e um
endpoint em `127.0.0.1` não pertence a provider nenhum. Só isso é afrouxado aqui,
e só para o teste: a navegação e o roteamento liberam a origem controlada, e o
`NetworkWriteGuard` continua intacto — toda escrita segue exigindo permit.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from jobsearch_agent.browser import PlaywrightSessionManager


class LoopbackSession(PlaywrightSessionManager):
    def __init__(self, origin: str):
        host = urlsplit(origin).hostname or "127.0.0.1"
        super().__init__(allowed_hosts={host})
        self.origin = origin

    def _guard_route(self, route):  # noqa: ANN001 - assinatura do Playwright
        if route.request.url.startswith(self.origin):
            if self.network_guard is not None and self.network_guard.inspect(route.request):
                self.network_guard.begin_read(route.request)
                route.continue_()
            else:
                route.abort("blockedbyclient")
            return
        super()._guard_route(route)

    def open(self, url: str) -> None:
        """Teste apenas: loopback não passa pela política de host pública."""
        self.page.goto(url, wait_until="domcontentloaded")
