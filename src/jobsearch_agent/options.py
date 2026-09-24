"""Rotulos de opcao: da resposta declarada para o vocabulario do ATS.

Um `combobox`/`select` nao aceita texto livre: o valor tem de ser UMA das opcoes
do board. As tabelas deste modulo dizem quais FORMAS podem significar o mesmo
valor declarado — "Immediately" e "Available immediately" sao a mesma coisa, e
"Job board" e "Job site" tambem.

Duas regras sustentam o desenho:

1. **Nada aqui inventa fato.** Cada forma e apenas outra maneira de escrever o
   MESMO valor ja declarado pelo candidato; a evidencia continua sendo a origem
   original (`preferences.notice_period`, `answer_policy:referral_source`, ...).
2. **Falhar alto e melhor que gravar parecido.** Quando nenhum candidato casa
   com as opcoes REAIS do widget, o preenchimento levanta
   `OPTION_NOT_FOUND_COMBOBOX_OPTION` em vez de escrever algo que o board nao
   reconhece — foi assim que "Immediately" (opcao real: "Available immediately")
   e "Latin America" (opcao real: "Latin America (Mexico, ...)") apareceram como
   respondidas sem nunca terem sido escolhidas.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Mapping

from .resolver import (
    EMPLOYMENT_TYPE_ALIASES,
    NOTICE_PERIOD_ALIASES,
    REGION_ALIASES,
    normalize_label,
)

#: Como cada origem declarada aparece escrita no ATS. A chave e o valor canonico
#: que o candidato pode declarar; as formas sao maneiras equivalentes de dizer.
REFERRAL_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "social_media": ("social media", "social network"),
    "job_board": ("job board", "job site", "job portal"),
    "employee_referral": ("employee referral", "referral", "referred by an employee"),
    "careers_page": ("careers page", "careers site", "company website"),
    "search_engine": ("search engine", "search"),
    "other": ("other",),
}

#: Situacao de sponsorship (nao e uma preferencia do candidato, e um FATO
#: derivado do pais onde ele mora), -> formas no ATS.
SPONSORSHIP_ALIASES: dict[str, tuple[str, ...]] = {
    "outside_us": (
        "Not applicable - I am located outside of the U.S.",
        "Not applicable",
        "Located outside",
    ),
    "no": ("no",),
    "yes": ("yes",),
}

#: Semantica/regra -> valores canonicos -> formas aceitas.
_TABLES: dict[str, Mapping[str, tuple[str, ...]]] = {
    "notice_period": NOTICE_PERIOD_ALIASES,
    "employment_type": EMPLOYMENT_TYPE_ALIASES,
    "region": REGION_ALIASES,
    "referral_source": REFERRAL_SOURCE_ALIASES,
    "sponsorship": SPONSORSHIP_ALIASES,
}


def option_key(semantic_type: str = "", supported_by: Sequence[str] = ()) -> str:
    """Qual tabela governa este campo.

    A regra da politica carrega a chave (`answer_policy:referral_source`); quando
    nao ha regra, a semantica do campo responde ("notice_period", "region").
    """
    for item in supported_by or ():
        text = str(item or "")
        if text.startswith("answer_policy:"):
            return text.split(":", 1)[1].strip()
    return str(semantic_type or "").strip()


def _canonical_for(table: Mapping[str, tuple[str, ...]], value: str) -> str:
    """Valor canonico que corresponde a este texto (chave ou qualquer forma)."""
    wanted = normalize_label(value)
    if not wanted:
        return ""
    for canonical, forms in table.items():
        if normalize_label(canonical) == wanted:
            return canonical
        if any(normalize_label(form) == wanted for form in forms):
            return canonical
    return ""


def option_forms(key: str, value: str) -> tuple[str, ...]:
    """Formas conhecidas para `value` na tabela `key` (vazio se desconhecido)."""
    table = _TABLES.get(str(key or "").strip())
    if not table:
        return ()
    canonical = _canonical_for(table, value)
    if not canonical:
        return ()
    return tuple(table[canonical])


def option_candidates(
    *,
    semantic_type: str = "",
    supported_by: Sequence[str] = (),
    value: str = "",
) -> tuple[str, ...]:
    """Textos a tentar, em ordem, contra as opcoes REAIS do widget.

    O valor declarado vem primeiro (se ele ja for a opcao do board, nada mais e
    tentado); depois entram as formas equivalentes.
    """
    declared = " ".join(str(value or "").split())
    candidates: list[str] = [declared] if declared else []
    for form in option_forms(option_key(semantic_type, supported_by), declared):
        if form not in candidates:
            candidates.append(form)
    return tuple(candidates)
