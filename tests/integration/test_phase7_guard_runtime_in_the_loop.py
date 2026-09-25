"""Fase 7 (CG-036): o loop usando o RUNTIME PUBLICO do challenge-guard.

A Fase 6 provou que trocar a politica inline pelo orquestrador do host nao muda o
comportamento. Esta fase troca o orquestrador do host pelo **runtime publico do
guard** (`challenge_guard.ChallengeRuntime`) e faz a mesma pergunta:

    o comportamento observavel mudou?

O contrato e o mesmo dos cenarios anteriores:

    desafio resolvido pela pessoa -> SUBMITTED, exatamente 1 escrita
    ninguem resolve                -> NEEDS_CAPTCHA (retomavel), 0 escritas
    submit nunca sai do alcance    -> o relay permanece travado durante a janela

A diferenca esta em QUEM faz o que: observacao, rounds, limites e validacao agora
sao do guard; a janela do operador, o relogio entre rounds e a evidencia duravel
continuam sendo do host (a ACL em `jobsearch_agent.challenge_acl` traduz o
resultado).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

from jobsearch_agent.live_view import OperatorCommand
from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import SUBMITTED, build_harness, remove_challenge_marker

pytestmark = pytest.mark.integration


def _solve_once(page, observed: list) -> None:
    """A pessoa resolve: o desafio sai da pagina (uma vez so)."""
    if any(entry.get("solved") for entry in observed):
        return
    if page.evaluate("() => !!document.getElementById('gate-challenge')"):
        remove_challenge_marker(page)
        observed.append({"solved": True})
    observed.append({"locked": page.evaluate("() => document.querySelectorAll('[data-liveview-locked]').length")})


def test_the_guard_runtime_resolves_and_the_loop_submits_once(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=3.0,
            poll_seconds=0.05,
            on_wait=_solve_once,
            runtime_handling=True,
        )

        result = harness.run()

        assert result.status == SUBMITTED, (result.status, result.reason)
        assert result.state is ApplicationState.SUBMITTED
        # Exactly-once: um POST, uma escrita autorizada.
        assert len(ats.posts) == 1
        assert harness.writes() == 1
        # A janela do operador travou o submit enquanto esteve aberta.
        assert harness.observed, "o hook de espera nunca rodou"
        assert all(entry.get("locked", 1) >= 1 for entry in harness.observed if "locked" in entry)
        assert harness.relay is not None and harness.relay.submit_locked is False
        # A evidencia duravel da janela e do host, e veio do runtime do guard.
        events = [(event.event, dict(event.payload)) for event in harness.database.list_application_events(harness.application().id)]
        kinds = [kind for kind, _payload in events]
        assert kinds.count("challenge_handoff_started") == 1
        finished = next(payload for kind, payload in events if kind == "challenge_handoff_finished")
        assert finished["handling"] == "continue"
        assert finished["resolved"] is True
        # Backend do guard para uma page entregue pelo host (sem sessao CDP).
        assert finished["backend"] == "page"


def test_the_guard_runtime_blocks_with_zero_writes_when_nobody_resolves(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=0.4,
            poll_seconds=0.05,
            on_wait=None,
            runtime_handling=True,
        )

        result = harness.run()

        assert result.status == "NEEDS_CAPTCHA"
        assert result.state is ApplicationState.NEEDS_CAPTCHA
        assert result.submission_attempted is False
        assert ats.posts == []
        assert harness.writes() == 0
        events = {event.event: event.payload for event in harness.database.list_application_events(harness.application().id)}
        blocked = events["challenge_detected_before_submit"]
        assert blocked["handling"] == "needs_human"
        assert blocked["final_status"] == "human_required"
        # O orcamento do guard e o relogio do host: com 400ms de janela e passos
        # de 50ms, ele reobservou varias vezes antes de desistir.
        assert blocked["rounds"] >= 2, blocked
        assert blocked["expired"] is False
        assert blocked["session_id"], "a proveniencia do guard precisa chegar ao host"


def test_the_operator_still_cannot_submit_through_the_guard_runtime(tmp_path: Path):
    """A trava de submit vale tambem no caminho do runtime publico."""

    def hijack(page, observed: list) -> None:
        if any(entry.get("sent") for entry in observed):
            return
        observed.append({"sent": True})
        box = page.evaluate(
            "() => { const b = document.querySelector('button[type=submit]');"
            " const r = b.getBoundingClientRect();"
            " return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}; }"
        )
        assert box and harness_relay is not None
        harness_relay.offer(OperatorCommand("click", x=int(box["x"]), y=int(box["y"])))
        harness_relay.offer(OperatorCommand("press", key="Enter"))

    harness_relay = None
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=0.4,
            poll_seconds=0.05,
            on_wait=hijack,
            runtime_handling=True,
        )
        harness_relay = harness.relay

        result = harness.run()

        assert result.submission_writes == 0
        assert ats.posts == []
        assert harness.relay is not None
        assert any(not entry.accepted for entry in harness.relay.results)


def test_a_window_left_open_by_a_restart_is_reconciled_on_the_runtime_path(tmp_path: Path):
    from jobsearch_agent.application import ApplicationService

    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=3.0,
            poll_seconds=0.05,
            on_wait=_solve_once,
            runtime_handling=True,
        )
        application = ApplicationService(harness.database).create_for_job(harness.job_id)
        harness.database.append_application_event(
            application.id,
            "challenge_handoff_started",
            {"session_id": "sessao-do-processo-morto", "provider": "recaptcha", "challenge_type": "checkbox"},
        )

        result = harness.run()

        assert result.status == SUBMITTED, (result.status, result.reason)
        events = [(event.event, dict(event.payload)) for event in harness.database.list_application_events(application.id)]
        kinds = [kind for kind, _payload in events]
        abandoned = [payload for kind, payload in events if kind == "challenge_handoff_abandoned"]
        assert len(abandoned) == 1, kinds
        assert abandoned[0]["reason"] == "process_restarted_with_window_open"
        assert kinds.index("challenge_handoff_abandoned") < kinds.index("application_preparing")
