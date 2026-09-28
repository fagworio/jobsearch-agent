"""Preenchimento REAL do formulario, com upload do curriculo. NAO submete.

Ordem importa: o curriculo sobe PRIMEIRO porque o Lever le o PDF e auto-preenche
o formulario depois; os fatos aprovados sao escritos em seguida e lidos de volta
para que o relatorio diga o que de fato ficou na pagina — nao o que se pretendeu.

Este modulo nao tem caminho de submit: nenhum clique em "Submit application"
existe aqui. `attempts` e `submission_writes` sao sempre 0.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .answers import AnswerLibrary, resolve
from .ats import find_apply_url, inspect_form
from .models import Field, Form, State


def _css(key: str) -> str:
    """Valor de atributo -> string CSS. `json.dumps` ja escapa aspas e barra."""
    return json.dumps(key)


@dataclass(frozen=True)
class FilledField:
    key: str
    prompt: str
    kind: str
    intended: str
    observed: str

    @property
    def verified(self) -> bool:
        return self.intended == self.observed

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "kind": self.kind,
            "intended": self.intended,
            "observed": self.observed,
            "verified": self.verified,
        }


@dataclass(frozen=True)
class Prepared:
    """Resultado do trabalho na pagina: pronto para enviar, ou faltando fato."""

    form: Form
    job_url: str
    apply_url: str
    filled: tuple[FilledField, ...] = ()
    missing: tuple[str, ...] = ()
    resume: str = ""
    resume_attached: bool = False
    notes: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ready(self) -> bool:
        return not self.reason


@dataclass(frozen=True)
class FillReport:
    state: State
    reason: str = ""
    job_url: str = ""
    apply_url: str = ""
    fields: int = 0
    filled: tuple[FilledField, ...] = ()
    missing: tuple[str, ...] = ()
    resume: str = ""
    resume_attached: bool = False
    notes: tuple[str, ...] = ()
    uploads: int = 0
    attempts: int = 0
    submission_writes: int = 0
    submitted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "job_url": self.job_url,
            "apply_url": self.apply_url,
            "fields": self.fields,
            "filled": {item.prompt: item.to_dict() for item in self.filled},
            "missing": list(self.missing),
            "resume": self.resume,
            "resume_attached": self.resume_attached,
            "notes": list(self.notes),
            "uploads": self.uploads,
            "attempts": self.attempts,
            "submission_writes": self.submission_writes,
            "submitted": self.submitted,
        }


def prepare_page(
    page: Any,
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    library: AnswerLibrary | None = None,
    resume: str = "",
    timeout_ms: float = 45_000,
) -> Prepared:
    """Navega vaga -> apply, sobe o curriculo, preenche e LE DE VOLTA."""
    page.goto(url, wait_until="load", timeout=timeout_ms)
    apply_url = find_apply_url(page.content(), url) or url
    if apply_url != url:
        page.goto(apply_url, wait_until="load", timeout=timeout_ms)

    form = inspect_form(page.content())
    if not form.fields:
        return Prepared(form=form, job_url=url, apply_url=apply_url, reason="form_not_found")

    resolution = resolve(form, approved=approved, profile=profile, rules=rules, library=library)
    if not resolution.complete:
        # Falta fato da pessoa: nao se toca no formulario.
        return Prepared(
            form=form,
            job_url=url,
            apply_url=apply_url,
            reason="missing_answer",
            missing=tuple(item.prompt for item in resolution.missing),
        )

    notes: list[str] = []
    resume_attached = False
    if resume:
        page.set_input_files(f'input[name={_css("resume")}]', resume)
        page.wait_for_timeout(4_000)  # o Lever le o PDF e auto-preenche
        attached = page.eval_on_selector(f'input[name={_css("resume")}]', "el => el.files.length")
        resume_attached = bool(attached)
        notes.append(f"resume upload: {attached} arquivo(s) anexado(s)")

    filled: list[FilledField] = []
    by_key = {item.key: item for item in form.fields}
    # `location` por ultimo: o autocomplete do Lever abre um dropdown e um
    # overlay aberto intercepta os cliques seguintes.
    ordered = sorted(resolution.answers.items(), key=lambda pair: pair[0] == "location")
    for key, value in ordered:
        item = by_key.get(key)
        if item is None:
            continue
        filled.append(_fill_field(page, item, value, notes))

    return Prepared(
        form=form,
        job_url=url,
        apply_url=apply_url,
        filled=tuple(filled),
        resume=resume,
        resume_attached=resume_attached,
        notes=tuple(notes),
    )


def fill(
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    library: AnswerLibrary | None = None,
    resume: str = "",
    timeout_ms: float = 45_000,
) -> FillReport:
    """Abre a vaga real, sobe o curriculo, preenche e LE DE VOLTA. Nao submete."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_context().new_page()
            prepared = prepare_page(
                page,
                url,
                approved=approved,
                profile=profile,
                rules=rules,
                library=library,
                resume=resume,
                timeout_ms=timeout_ms,
            )
            return FillReport(
                state=State.NEEDS_INPUT if prepared.reason else State.READY,
                reason=prepared.reason,
                job_url=prepared.job_url,
                apply_url=prepared.apply_url,
                fields=len(prepared.form.fields),
                filled=prepared.filled,
                missing=prepared.missing,
                resume=prepared.resume,
                resume_attached=prepared.resume_attached,
                notes=prepared.notes,
                uploads=1 if prepared.resume_attached else 0,
            )
        finally:
            browser.close()


def _read_hidden(page: Any, selector: str) -> str:
    """Valor de um input que pode nem existir ainda (ex.: selectedLocation)."""
    try:
        return page.input_value(selector)
    except Exception:  # noqa: BLE001 - ausencia do no e uma observacao, nao um erro
        return ""


def _fill_field(page: Any, item: Field, value: str, notes: list[str]) -> FilledField:
    """Escreve um campo e devolve o que a pagina REALMENTE mostra depois."""
    selector = f'[name={_css(item.key)}]'
    kind = item.kind

    if kind in {"checkbox", "radio"} and item.options:
        option = f'{selector}[value={_css(value)}]'
        page.check(option)
        observed = value if page.is_checked(option) else "unchecked"
        return FilledField(item.key, item.prompt, kind, value, observed)

    if kind in {"checkbox", "radio"}:
        page.check(selector)
        observed = value if page.is_checked(selector) else "unchecked"
        return FilledField(item.key, item.prompt, kind, value, observed)

    if item.options:
        page.select_option(selector, value)
        return FilledField(item.key, item.prompt, kind, value, page.input_value(selector))

    page.fill(selector, value)
    observed = page.input_value(selector)
    if item.key == "location":
        page.wait_for_timeout(1_500)
        notes.append(
            "location: selectedLocation=%r, sugestoes=%d"
            % (
                _read_hidden(page, f'input[name={_css("selectedLocation")}]'),
                page.locator(".dropdown-results > *").count(),
            )
        )
        try:
            page.locator(selector).blur()  # fecha o dropdown antes dos cliques
        except Exception:  # noqa: BLE001 - blur e higiene, nao requisito
            notes.append("location: blur indisponivel")
    return FilledField(item.key, item.prompt, kind, value, observed)
