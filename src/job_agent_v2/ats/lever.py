"""Leitura do formulario Lever a partir do HTML real.

A licao que este modulo carrega (descoberta contra a pagina real da Spotify):
o PROMPT da pergunta vive em `.application-label .text`, e cada OPCAO tem o
proprio `<label>`. Usar o label do primeiro elemento como label do campo fazia a
pergunta virar `"No"` e a busca de resposta procurar a chave errada.

`_css_string_escape` foi COPIADO do V1 (com teste proprio aqui): a correcao e
conhecimento, nao dependencia. Nada deste modulo importa `jobsearch_agent`.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Field, Form

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_APPLY_TEXT = re.compile(r"^\s*apply\b", re.IGNORECASE)
#: Marcador de obrigatorio do label: "✱" na pagina real, "*" nas fixtures.
_REQUIRED_MARK = "*✱"


def _css_string_escape(value: str) -> str:
    """Escapa um valor para dentro de uma CSS string (`[id="..."]`).

    Aspas e barra invertida tem escape classico; controles/newlines viram
    `backslash + hex + espaco`, que e o que o parser CSS aceita. O valor NAO e
    normalizado: colapsar espaco apontaria para outro elemento.
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return _CONTROL.sub(lambda m: "\\%x " % ord(m.group(0)), text)


def _prompt_text(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split()).strip(_REQUIRED_MARK).strip()


def _card_prompt(element: Tag) -> str:
    """Prompt do CARD: so a pergunta, nunca o texto de uma opcao.

    Preferencia pelo `.application-label` (e, quando existe, pelo `.text` dentro
    dele). O `<label>` que envolve o card carrega tambem o campo e as mensagens
    de validacao: no documento real da Spotify isso fazia o prompt da
    localizacao virar "Current location ✱ No location found. Try entering a
    different location Loading", e a chave de identidade sairia poluida.
    """
    question = element.find_parent(class_="application-question")
    if question is None:
        return ""
    for selector in (".application-label .text", ".application-label"):
        node = question.select_one(selector)
        if node is not None:
            text = _prompt_text(node)
            if text:
                return text
    return ""


def _option_label(element: Tag) -> str:
    """Texto da OPCAO: o `<label>` que envolve o proprio input."""
    parent = element.find_parent("label")
    if parent is not None:
        return " ".join(parent.get_text(" ", strip=True).split())
    return str(element.get("value") or "").strip()


def _option_value(element: Tag) -> str:
    return str(element.get("value") or _option_label(element) or "").strip()


def find_apply_url(html: str, base_url: str = "") -> str | None:
    """URL do formulario, LIDA do link real da pagina da vaga.

    Descoberto contra a Spotify real: a pagina da vaga tem
    `<a data-qa="show-page-apply" href=".../apply">apply for this job</a>` e
    ZERO inputs; o formulario vive na rota apontada pelo link. O href e lido em
    vez de concatenar `/apply`, para seguir o provider em vez de inventar
    convencao. `None` quando a propria pagina ja e o formulario.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        is_apply = (
            str(anchor.get("data-qa") or "") == "show-page-apply"
            or href.rstrip("/").endswith("/apply")
            or bool(_APPLY_TEXT.match(anchor.get_text(" ", strip=True)))
        )
        if is_apply:
            return urljoin(base_url, href)
    return None


def inspect_form(html: str) -> Form:
    """HTML Lever -> campos com prompt, opcoes, tipo e obrigatoriedade."""
    soup = BeautifulSoup(html or "", "html.parser")
    grouped: dict[str, list[Tag]] = {}
    order: list[str] = []
    for element in soup.find_all(["input", "textarea", "select"]):
        if str(element.get("type", "")).casefold() in {"hidden", "submit", "button"}:
            continue
        name = str(element.get("name") or element.get("id") or "").strip()
        if not name:
            continue
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(element)

    fields: list[Field] = []
    for name in order:
        group = grouped[name]
        first = group[0]
        kind = str(first.get("type", "") or first.name or "text").casefold()
        prompt = _card_prompt(first) or _option_label(first) or str(first.get("aria-label") or name)
        if kind in {"checkbox", "radio"} and len(group) > 1:
            options = tuple(dict.fromkeys(_option_value(item) for item in group))
        elif first.name == "select":
            options = tuple(
                str(option.get("value") or option.get_text(strip=True))
                for option in first.find_all("option")
                if str(option.get("value") or "").strip()
            )
        else:
            options = ()
        fields.append(
            Field(
                key=name,
                prompt=prompt,
                options=options,
                required=any(item.has_attr("required") for item in group),
                kind=kind,
            )
        )
    return Form(tuple(fields))
