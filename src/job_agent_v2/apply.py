"""Fluxo principal do V2-001A — legivel de cima para baixo."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from .answers import resolve
from .ats import find_apply_url, inspect_form
from .browser import open_page_html
from .models import ApplyResult, State

PageLoader = Callable[[str], str]


def apply(
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    open_page: PageLoader | None = None,
) -> ApplyResult:
    """Le a vaga real e decide. Para antes de qualquer escrita.

    Nao ha upload, submit, intent, attempt ou recovery: o incremento termina
    deliberadamente em NEEDS_INPUT quando falta um fato da pessoa.

    A pagina da VAGA nao e o formulario. O link real `apply for this job` e
    seguido; se a propria url ja for o formulario (ou nao houver link), o
    documento lido e inspecionado direto.
    """
    loader = open_page or open_page_html
    job_html = loader(url)
    apply_url = find_apply_url(job_html, url) or url
    form_html = job_html if apply_url == url else loader(apply_url)

    form = inspect_form(form_html)
    if not form.fields:
        # 0 campos NAO e formulario completo: e formulario que nao foi achado.
        return ApplyResult(
            state=State.NEEDS_INPUT,
            reason="form_not_found",
            job_url=url,
            apply_url=apply_url,
        )

    resolution = resolve(form, approved=approved, profile=profile, rules=rules)
    if not resolution.complete:
        return ApplyResult(
            state=State.NEEDS_INPUT,
            reason="missing_answer",
            missing=resolution.missing,
            answers=resolution.answers,
            fields=len(form.fields),
            job_url=url,
            apply_url=apply_url,
        )
    return ApplyResult(
        state=State.READY,
        answers=resolution.answers,
        fields=len(form.fields),
        job_url=url,
        apply_url=apply_url,
    )
