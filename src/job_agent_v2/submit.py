"""Envio REAL do curriculo. Exatamente uma vez, ou nenhuma.

A fronteira e a licao do P0 do V1, reduzida ao minimo:

1. `write_possible_at` e PERSISTIDO (com fsync) imediatamente ANTES do clique;
2. um POST NO ATS observado sem confirmacao e `SUBMIT_UNKNOWN` e, numa
   reexecucao, o marcador existente RECUSA um novo envio (fail-closed);
3. so ha reenvio quando o proprio documento provou que nenhum POST saiu para o
   ATS — um POST para `api.hcaptcha.com` NAO e escrita na candidatura.

Captcha nao e tocado: sem solver, sem stealth, sem bypass. Se o envio parar num
desafio hCaptcha, o resultado honesto e `HUMAN_REQUIRED` — e com `human_wait_ms`
a sessao fica aberta o tempo necessario para uma PESSOA resolver, sem segundo
clique (o callback do provedor envia o formulario).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
from typing import Any

from .fill import prepare_page

SUBMIT_BUTTON = "#btn-submit"
POST_SUBMIT_SETTLE_MS = 20_000
CAPTCHA_SELECTOR = 'iframe[src*="hcaptcha"], .hcaptcha, div[class*="hcaptcha"]'
CONFIRMATION = re.compile(
    r"(thank you for applying|thanks for applying|application (?:has been |was )?(?:submitted|received)"
    r"|we(?:'ve| have) received your application|your application has been received)",
    re.IGNORECASE,
)


class SubmitState(str, Enum):
    SUBMITTED = "SUBMITTED"                # confirmado pelo documento final
    HUMAN_REQUIRED = "HUMAN_REQUIRED"      # captcha/desafio: so uma pessoa resolve
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"      # POST no ATS sem confirmacao: nao repetir
    NO_WRITE = "NO_WRITE"                  # nenhum POST no ATS: repetir e seguro
    REFUSED = "REFUSED"                    # marcador anterior impede novo envio


@dataclass(frozen=True)
class SubmitReport:
    state: SubmitState
    reason: str = ""
    job_url: str = ""
    apply_url: str = ""
    fields: int = 0
    verified: int = 0
    resume: str = ""
    resume_attached: bool = False
    marker: str = ""
    write_possible_at: str = ""
    post_requests: tuple[str, ...] = ()
    other_post_requests: tuple[str, ...] = ()
    final_url: str = ""
    evidence: str = ""
    captcha: bool = False
    notes: tuple[str, ...] = ()
    attempts: int = 0
    submission_writes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "job_url": self.job_url,
            "apply_url": self.apply_url,
            "fields": self.fields,
            "verified": self.verified,
            "resume": self.resume,
            "resume_attached": self.resume_attached,
            "marker": self.marker,
            "write_possible_at": self.write_possible_at,
            "post_requests": list(self.post_requests),
            "other_post_requests": list(self.other_post_requests),
            "final_url": self.final_url,
            "evidence": self.evidence,
            "captcha": self.captcha,
            "notes": list(self.notes),
            "attempts": self.attempts,
            "submission_writes": self.submission_writes,
        }


def marker_path(store: str, job_url: str) -> Path:
    key = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:16]
    return Path(store) / f"{key}.json"


def read_marker(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - marcador ilegivel e tratado como bloqueio
        return {"outcome": "UNREADABLE"}
    return payload if isinstance(payload, dict) else {"outcome": "UNREADABLE"}


def _persist(path: Path, payload: dict[str, Any]) -> None:
    """Grava e forca o disco antes de qualquer escrita na rede."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def _host(url: str) -> str:
    match = re.match(r"https?://([^/]+)", str(url))
    return match.group(1).lower() if match else ""


def _split_posts(posts: list[str], apply_url: str) -> tuple[list[str], list[str]]:
    """Separa POST no ATS de POST de terceiro (captcha, telemetria, fontes)."""
    host = _host(apply_url)
    ats = [item for item in posts if _host(item.split(" ", 1)[-1]) == host]
    others = [item for item in posts if item not in ats]
    return ats, others


def _may_retry(existing: dict[str, Any]) -> bool:
    """Reenvia apenas quando o documento provou que nenhum POST saiu para o ATS."""
    if not existing:
        return True
    outcome = str(existing.get("outcome") or "")
    if outcome in {"SUBMITTED", "UNREADABLE"}:
        return False
    if not outcome and existing.get("write_possible_at"):
        return False  # clique pode ter saido sem observacao: fail-closed
    ats, _ = _split_posts(
        [str(item) for item in existing.get("post_requests") or ()],
        str(existing.get("apply_url") or ""),
    )
    return not ats


def _refusal(existing: dict[str, Any], path: Path) -> SubmitReport:
    outcome = str(existing.get("outcome") or "")
    return SubmitReport(
        state=SubmitState.REFUSED,
        reason="already_submitted" if outcome == "SUBMITTED" else "previous_attempt_unresolved",
        job_url=str(existing.get("job_url") or ""),
        apply_url=str(existing.get("apply_url") or ""),
        marker=str(path),
        write_possible_at=str(existing.get("write_possible_at") or ""),
        post_requests=tuple(str(item) for item in existing.get("post_requests") or ()),
        evidence=str(existing.get("evidence") or ""),
        submission_writes=1,
    )


def submit(
    url: str,
    *,
    approved: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    profile: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    rules: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    resume: str = "",
    store: str = "data/v2-submissions",
    settle_ms: int = POST_SUBMIT_SETTLE_MS,
    human_wait_ms: int = 0,
    headless: bool = True,
    timeout_ms: float = 45_000,
) -> SubmitReport:
    """Preenche de verdade e clica em enviar UMA vez. Nao repete em duvida."""
    from playwright.sync_api import sync_playwright

    path = marker_path(store, url)
    existing = read_marker(path)
    if not _may_retry(existing):
        return _refusal(existing, path)

    posts: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless, args=["--no-sandbox"])
        try:
            page = browser.new_context().new_page()
            page.on(
                "request",
                lambda request: posts.append(f"{request.method} {request.url}")
                if request.method.upper() == "POST"
                else None,
            )
            prepared = prepare_page(
                page,
                url,
                approved=approved,
                profile=profile,
                rules=rules,
                resume=resume,
                timeout_ms=timeout_ms,
            )
            if not prepared.ready:
                return SubmitReport(
                    state=SubmitState.NO_WRITE,
                    reason=prepared.reason,
                    job_url=prepared.job_url,
                    apply_url=prepared.apply_url,
                    fields=len(prepared.form.fields),
                    marker=str(path),
                )

            verified = sum(1 for item in prepared.filled if item.verified)
            upload_posts = list(posts)  # POSTs do upload do curriculo
            posts.clear()  # o que interessa e o que sair DEPOIS do clique

            write_possible_at = datetime.now(timezone.utc).isoformat()
            record: dict[str, Any] = {
                "job_url": prepared.job_url,
                "apply_url": prepared.apply_url,
                "resume": prepared.resume,
                "resume_sha256": _sha256(prepared.resume),
                "fields": len(prepared.form.fields),
                "verified": verified,
                "write_possible_at": write_possible_at,
                "outcome": "",
                "post_requests": [],
                "other_post_requests": [],
            }
            _persist(path, record)  # ANTES do clique: a fronteira e persistida

            page.click(SUBMIT_BUTTON)
            page.wait_for_timeout(settle_ms)
            final_url, content, captcha, confirmation = _page_state(page)
            if human_wait_ms and captcha and not confirmation:
                # Sessao viva para uma PESSOA resolver o desafio. Sem 2o clique:
                # o callback do provedor e quem envia o formulario.
                page.wait_for_timeout(human_wait_ms)
                final_url, content, captcha, confirmation = _page_state(page)

            ats_posts, other_posts = _split_posts(posts, prepared.apply_url)
            state, reason = _classify(ats_posts, confirmation, captcha)
            evidence = confirmation.group(0) if confirmation else _excerpt(content, final_url)

            record.update(
                outcome=state.value,
                reason=reason,
                final_url=final_url,
                post_requests=ats_posts,
                other_post_requests=other_posts,
                captcha=captcha,
                confirmation=bool(confirmation),
                form_present='id="application-form"' in content,
                evidence=evidence,
                clicked_at=datetime.now(timezone.utc).isoformat(),
            )
            _persist(path, record)

            return SubmitReport(
                state=state,
                reason=reason,
                job_url=prepared.job_url,
                apply_url=prepared.apply_url,
                fields=len(prepared.form.fields),
                verified=verified,
                resume=prepared.resume,
                resume_attached=prepared.resume_attached,
                marker=str(path),
                write_possible_at=write_possible_at,
                post_requests=tuple(ats_posts),
                other_post_requests=tuple(other_posts),
                final_url=final_url,
                evidence=evidence,
                captcha=captcha,
                notes=prepared.notes + (f"POSTs antes do clique (upload): {len(upload_posts)}",),
                attempts=1,
                submission_writes=1 if ats_posts else 0,
            )
        finally:
            browser.close()


def _classify(
    ats_posts: list[str], confirmation: re.Match[str] | None, captcha: bool
) -> tuple[SubmitState, str]:
    """Confirmacao > POST no ATS sem confirmacao > captcha > nenhum POST."""
    if confirmation:
        return SubmitState.SUBMITTED, "confirmation_observed"
    if ats_posts:
        return SubmitState.SUBMIT_UNKNOWN, "post_without_confirmation"
    if captcha:
        return SubmitState.HUMAN_REQUIRED, "challenge_requires_person"
    return SubmitState.NO_WRITE, "no_post_observed"


def _page_state(page: Any) -> tuple[str, str, bool, re.Match[str] | None]:
    content = _safe_content(page)
    return page.url, content, _captcha_present(page), CONFIRMATION.search(content)


def _sha256(path: str) -> str:
    if not path or not Path(path).exists():
        return ""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _safe_content(page: Any) -> str:
    try:
        return page.content()
    except Exception:  # noqa: BLE001 - navegacao em curso nao e erro de logica
        return ""


def _captcha_present(page: Any) -> bool:
    try:
        return page.locator(CAPTCHA_SELECTOR).count() > 0
    except Exception:  # noqa: BLE001
        return False


def _excerpt(content: str, final_url: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content)
    text = " ".join(text.split())
    return f"url={final_url} | {text[:280]}"
