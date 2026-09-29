"""Fluxo principal do V2-001A — legivel de cima para baixo."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from .answers import AnswerLibrary, resolve
from .ats import find_apply_url, inspect_form
from .ats.greenhouse import GreenhouseAdapter
from .browser import NativeMessagingClient
from .facts import FactStore
from .models import ApplyResult, State

PageLoader = Callable[[str], str]


def apply(
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    library: AnswerLibrary | None = None,
    facts: FactStore | None = None,
    open_page: PageLoader | None = None,
) -> ApplyResult:
    """Le a vaga real e decide. Para antes de qualquer escrita.

    Nao ha upload, submit, intent, attempt ou recovery: o incremento termina
    deliberadamente em NEEDS_INPUT quando falta um fato da pessoa.

    A pagina da VAGA nao e o formulario. O link real `apply for this job` e
    seguido; se a propria url ja for o formulario (ou nao houver link), o
    documento lido e inspecionado direto.
    """
    if open_page is None:
        return _apply_live(
            url,
            approved=approved,
            profile=profile,
            rules=rules,
            library=library,
            facts=facts,
        )

    loader = open_page
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

    resolution = resolve(form, approved=approved, profile=profile, rules=rules, library=library, facts=facts)
    if not resolution.complete:
        return ApplyResult(
            state=State.NEEDS_INPUT,
            reason="missing_answer",
            missing=resolution.missing,
            answers=resolution.answers,
            resolved_from=resolution.resolved_from,
            resolved_fact_ids=resolution.resolved_fact_ids,
            missing_fact_ids=resolution.missing_fact_ids,
            missing_questions=resolution.missing_questions,
            fields=len(form.fields),
            job_url=url,
            apply_url=apply_url,
        )
    return ApplyResult(
        state=State.READY,
        answers=resolution.answers,
        resolved_from=resolution.resolved_from,
        resolved_fact_ids=resolution.resolved_fact_ids,
        fields=len(form.fields),
        job_url=url,
        apply_url=apply_url,
    )


def _apply_live(
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None,
    library: AnswerLibrary | None,
    facts: FactStore | None,
) -> ApplyResult:
    """Inspeciona a aba do Chrome normal através da extensão carregada."""

    try:
        with NativeMessagingClient() as browser:
            auth = browser.auth_state()
            if auth.get("state") in {"LOGIN_REQUIRED", "LOGIN_PENDING"}:
                return ApplyResult(
                    state=State.NEEDS_INPUT,
                    reason="login_required" if auth.get("state") == "LOGIN_REQUIRED" else "login_pending",
                    job_url=url,
                    apply_url=url,
                )
            snapshot = browser.inspect_form()
    except (OSError, RuntimeError) as exc:
        return ApplyResult(
            state=State.NEEDS_INPUT,
            reason=f"browser_unavailable: {exc}",
            job_url=url,
            apply_url=url,
        )
    try:
        form = GreenhouseAdapter().to_form(snapshot)
    except Exception as exc:  # noqa: BLE001 - resposta do browser deve virar estado
        return ApplyResult(
            state=State.NEEDS_INPUT,
            reason="form_not_found" if not snapshot.get("fields") else str(exc),
            job_url=url,
            apply_url=str(snapshot.get("url") or url),
        )

    resolution = resolve(form, approved=approved, profile=profile, rules=rules, library=library, facts=facts)
    if not resolution.complete:
        return ApplyResult(
            state=State.NEEDS_INPUT,
            reason="missing_answer",
            missing=resolution.missing,
            answers=resolution.answers,
            resolved_from=resolution.resolved_from,
            resolved_fact_ids=resolution.resolved_fact_ids,
            missing_fact_ids=resolution.missing_fact_ids,
            missing_questions=resolution.missing_questions,
            fields=len(form.fields),
            job_url=url,
            apply_url=str(snapshot.get("url") or url),
        )
    return ApplyResult(
        state=State.READY,
        answers=resolution.answers,
        resolved_from=resolution.resolved_from,
        resolved_fact_ids=resolution.resolved_fact_ids,
        fields=len(form.fields),
        job_url=url,
        apply_url=str(snapshot.get("url") or url),
    )
