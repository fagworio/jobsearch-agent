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
from .ats.greenhouse import GreenhouseAdapter
from .artifacts import ResumeArtifact
from .browser import NativeMessagingClient
from .facts import FactStore
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
    facts: FactStore | None = None,
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

    resolution = resolve(form, approved=approved, profile=profile, rules=rules, library=library, facts=facts)
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
    facts: FactStore | None = None,
    resume: str = "",
    timeout_ms: float = 45_000,
) -> FillReport:
    """Preenche a aba ativa do Chrome através da extensão; nunca submete."""

    del timeout_ms  # o timeout da sessão é controlado pelo cliente Native Messaging
    try:
        with NativeMessagingClient() as browser:
            auth = browser.auth_state()
            if auth.get("state") in {"LOGIN_REQUIRED", "LOGIN_PENDING"}:
                return FillReport(
                    state=State.NEEDS_INPUT,
                    reason="login_required" if auth.get("state") == "LOGIN_REQUIRED" else "login_pending",
                    job_url=url,
                    resume=resume,
                )
            snapshot = browser.inspect_form()
            form = GreenhouseAdapter().to_form(snapshot)
            resolution = resolve(form, approved=approved, profile=profile, rules=rules, library=library, facts=facts)
            apply_url = str(snapshot.get("url") or url)
            if not resolution.complete:
                return FillReport(
                    state=State.NEEDS_INPUT,
                    reason="missing_answer",
                    job_url=url,
                    apply_url=apply_url,
                    fields=len(form.fields),
                    missing=tuple(item.prompt for item in resolution.missing),
                    resume=resume,
                )

            notes: list[str] = []
            resume_attached = False
            uploads = 0
            if resume:
                artifact = ResumeArtifact.from_path(resume)
                resume_field = next((item for item in form.fields if item.kind == "file"), None)
                if resume_field is None:
                    return FillReport(
                        state=State.NEEDS_INPUT,
                        reason="resume_field_not_found",
                        job_url=url,
                        apply_url=apply_url,
                        fields=len(form.fields),
                        resume=resume,
                    )
                uploaded = browser.upload_artifact(artifact.to_payload(resume_field.key))
                resume_attached = _snapshot_field_value(uploaded, resume_field.key) != ""
                if not resume_attached:
                    raise RuntimeError("resume upload read-back was empty")
                uploads = 1
                notes.append(f"resume upload read-back: {resume_field.key}")

            filled: list[FilledField] = []
            for field in form.fields:
                if field.key not in resolution.answers:
                    continue
                value = resolution.answers[field.key]
                action = _action_for(field, value)
                observed_snapshot = browser.fill_form({"field_id": field.key, "action": action, "value": value})
                observed = _snapshot_field_value(observed_snapshot, field.key)
                filled.append(FilledField(field.key, field.prompt, field.kind, value, observed))

            read_back = browser.read_form()
            for item in filled:
                actual = _snapshot_field_value(read_back, item.key)
                if actual != item.intended:
                    raise RuntimeError(
                        f"FIELD_MISMATCH: expected {item.intended} but read {actual} for {item.key}"
                    )
            challenge = browser.challenge_state()
            state = State.WAITING_HUMAN if challenge.get("state") == "BLOCKING" else State.FILLED
            return FillReport(
                state=state,
                job_url=url,
                apply_url=apply_url,
                fields=len(form.fields),
                filled=tuple(filled),
                resume=resume,
                resume_attached=resume_attached,
                notes=tuple(notes),
                uploads=uploads,
            )
    except Exception as exc:  # noqa: BLE001 - browser boundary becomes a report
        return FillReport(state=State.NEEDS_INPUT, reason=str(exc), job_url=url, resume=resume)


def _action_for(field: Field, value: str) -> str:
    if field.kind in {"select", "combobox"} or field.options:
        return "select"
    if field.kind in {"checkbox", "radio"}:
        return "check"
    return "set"


def _snapshot_field_value(snapshot: Mapping[str, Any], field_id: str) -> str:
    fields = snapshot.get("fields", [])
    if not isinstance(fields, list):
        return ""
    for item in fields:
        if isinstance(item, Mapping) and item.get("id") == field_id:
            if item.get("checked") is True:
                return str(item.get("value") or "true")
            return str(item.get("value") or "")
    return ""


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
