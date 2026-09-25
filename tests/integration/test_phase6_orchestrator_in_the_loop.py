"""Fase 6: o ORQUESTRADOR dentro do `ApplicationLoop`, com o guard real.

O loop tinha a politica de challenge inline (o gate). Aqui ele passa a chamar a
`ChallengeIntegration`, que compõe observador (guard real) + engine + estrategia
de relay + validador, e roda o `ChallengeOrchestrator`.

O contrato e o mesmo dos cenarios anteriores — e e isso que se prova: trocar a
politica inline pelo orquestrador NAO muda o comportamento observavel.

    desafio resolvido pela pessoa -> SUBMITTED, exatamente 1 escrita
    ninguem resolve                -> NEEDS_CAPTCHA (retomavel), 0 escritas
    submit nunca sai do alcance    -> o relay permanece travado durante a janela
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
    """A pessoa resolve: o desafio sai da página (uma vez só)."""
    if any(entry.get("solved") for entry in observed):
        return
    marker = page.evaluate("() => !!document.getElementById('gate-challenge')")
    if marker:
        remove_challenge_marker(page)
        observed.append({"solved": True})
    observed.append({"locked": page.evaluate("() => document.querySelectorAll('[data-liveview-locked]').length")})


def test_the_orchestrator_resolves_a_real_challenge_and_the_loop_submits_once(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=3.0,
            poll_seconds=0.05,
            on_wait=_solve_once,
            orchestrator_handling=True,
        )

        result = harness.run()

        assert result.status == SUBMITTED, (result.status, result.reason, result.to_dict())
        assert result.state is ApplicationState.SUBMITTED
        # Exactly-once: um POST, uma escrita autorizada.
        assert len(ats.posts) == 1
        assert harness.writes() == 1
        # A janela do operador travou o submit enquanto esteve aberta.
        assert harness.observed, "o hook de espera nunca rodou"
        assert all(entry.get("locked", 1) >= 1 for entry in harness.observed if "locked" in entry)
        assert harness.relay is not None and harness.relay.submit_locked is False


def test_the_orchestrator_blocks_with_zero_writes_when_nobody_resolves(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=0.4,
            poll_seconds=0.05,
            on_wait=None,
            orchestrator_handling=True,
        )

        result = harness.run()

        assert result.status == "NEEDS_CAPTCHA"
        assert result.state is ApplicationState.NEEDS_CAPTCHA
        assert result.submission_attempted is False
        assert ats.posts == []
        assert harness.writes() == 0
        # O journal do loop registra o outcome do ORQUESTRADOR (rounds, status final).
        events = {event.event: event.payload for event in harness.database.list_application_events(harness.application().id)}
        blocked = events["challenge_detected_before_submit"]
        assert blocked["handling"] == "needs_human"
        assert blocked["rounds"] == 1
        assert blocked["final_status"] == "human_required"


def test_a_solved_challenge_keeps_the_material_untouched(tmp_path: Path):
    """O desafio pode mudar o DOM; o material aprovado, nunca."""
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=3.0,
            poll_seconds=0.05,
            on_wait=_solve_once,
            orchestrator_handling=True,
        )

        result = harness.run()

        assert result.status == SUBMITTED
        assert result.resume_sha256 == harness.resume_sha256
        application = harness.application()
        assert result.answers_fingerprint == application.context["journey"]["answers_fingerprint"]


def test_the_operator_still_cannot_submit_through_the_orchestrator_path(tmp_path: Path):
    """A trava de submit vale nos dois caminhos (gate e orquestrador)."""

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
            orchestrator_handling=True,
        )
        harness_relay = harness.relay

        result = harness.run()

        assert result.submission_writes == 0
        assert ats.posts == []
        relay = harness.relay
        assert relay is not None
        assert any(not entry.accepted for entry in relay.results)
