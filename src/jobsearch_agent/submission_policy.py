"""Politica de canal por dominio — e a trava que impede POST sem autorizacao.

Esta tabela e a decisao de PRODUTO: para cada dominio, por onde a candidatura
sai. Ela e dado explicito, versionado e testado — nao heuristica.

A regra que mais importa aqui, e que foi MEDIDA antes de ser escrita:

    canal `API` exige credencial autorizada declarada para aquele dominio.

Sem isso o adapter faria um POST nao autorizado a um terceiro. Medido contra o
endpoint publico do Greenhouse:

    GET  /v1/boards/<board>/jobs/<id>            -> 200 (leitura publica)
    POST /v1/boards/<board>/jobs/<id> (sem auth) -> 401 "HTTP Basic: Access denied"

Ou seja: o endpoint de candidatura EXISTE e exige credencial da empresa/parceiro.
Um adapter sem credencial nao "tenta e ve no que da": ele degrada para o canal de
browser, onde o unico caminho de escrita continua sendo o `NetworkWriteGuard`,
com intent e exactly-once.

LinkedIn e Indeed nao aparecem como `API` de proposito: nao ha API publica de
candidatura para qualquer vaga, e automatizar apply nesses sites e o que o
ADR 0001 recusa. Eles sao `HANDOFF` (a pessoa conclui no navegador dela).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum


class Channel(str, Enum):
    """Por onde a candidatura sai."""

    API = "api"          # API do ATS, com credencial autorizada
    BROWSER = "browser"  # formulario no browser, com guard e handoff
    HANDOFF = "handoff"  # a pessoa conclui no navegador dela
    SKIP = "skip"        # a politica decide nao candidatar


#: Dominios cujo POST de candidatura exige credencial de empresa/parceiro.
#: Continuam nesta tabela porque, COM credencial, sao o melhor canal.
_API_CANDIDATES: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "workable.com",
    "ashbyhq.com",
    "smartrecruiters.com",
)

#: Dominios onde nao existe API publica de candidatura (ou onde automatizar e
#: recusado por politica): a pessoa conclui.
_HANDOFF_ONLY: tuple[str, ...] = (
    "linkedin.com",
    "indeed.com",
)


@dataclass(frozen=True)
class DomainPolicy:
    """Tabela de canais, com degradacao obrigatoria quando falta credencial."""

    api_domains: tuple[str, ...] = _API_CANDIDATES
    handoff_domains: tuple[str, ...] = _HANDOFF_ONLY
    skip_domains: tuple[str, ...] = ()

    @staticmethod
    def _matches(domain: str, pattern: str) -> bool:
        normalized = str(domain or "").strip().casefold()
        candidate = str(pattern or "").strip().casefold()
        if not normalized or not candidate:
            return False
        return normalized == candidate or normalized.endswith("." + candidate)

    def channel_for(self, domain: str, *, authorized_domains: Collection[str] = ()) -> Channel:
        """Canal para o dominio, dado o que ja esta AUTORIZADO por credencial.

        `authorized_domains` sao os dominios com credencial configurada (a
        chave da API do ATS, tipicamente). Um dominio da lista de API sem
        credencial NAO vira `API`: degrada para `BROWSER`.
        """
        for pattern in self.skip_domains:
            if self._matches(domain, pattern):
                return Channel.SKIP
        for pattern in self.handoff_domains:
            if self._matches(domain, pattern):
                # Nem com credencial: nao ha API publica de candidatura aqui.
                return Channel.HANDOFF
        for pattern in self.api_domains:
            if self._matches(domain, pattern):
                authorized = any(self._matches(item, pattern) or self._matches(pattern, item) for item in authorized_domains)
                return Channel.API if authorized else Channel.BROWSER
        return Channel.BROWSER

    def api_domains_authorized(self, authorized_domains: Collection[str]) -> tuple[str, ...]:
        """Subconjunto da tabela de API que tem credencial — para diagnostico."""
        return tuple(
            pattern
            for pattern in self.api_domains
            if any(self._matches(item, pattern) or self._matches(pattern, item) for item in authorized_domains)
        )


#: Politica padrao. Sobrescrevivel por configuracao (mesma estrutura).
DEFAULT_POLICY = DomainPolicy()


__all__ = ["Channel", "DEFAULT_POLICY", "DomainPolicy"]
