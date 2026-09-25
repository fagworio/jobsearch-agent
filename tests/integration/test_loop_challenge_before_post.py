"""Cenário canônico #1 — challenge ANTES do POST.

Duas variantes, ambas com o guard real num Chromium real:

A. a pessoa resolve dentro do orçamento  → segue, exatamente UM POST, SUBMITTED;
B. ninguém resolve dentro do orçamento   → ZERO POST, estado retomável.

O guard que observa é o `challenge-guard`; o gate não clica em nada. O que o
teste faz é remover o marcador do DOM na thread do loop (o mesmo efeito de um
clique humano), via hook de espera.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

pytestmark = pytest.mark.integration

from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import SUBMITTED, build_harness, remove_challenge_marker


def test_a_challenge_resolved_by_a_person_lets_the_single_write_through(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=3.0,
            on_wait=remove_challenge_marker,
        )

        assert harness.marker_present() is False  # ainda não abriu browser
        result = harness.run()

        assert result.status == SUBMITTED, (result.status, result.reason, result.to_dict())
        assert result.state is ApplicationState.SUBMITTED
        # Exatamente um POST saiu, e ele chegou ao servidor.
        assert harness.writes() == 1
        assert len(ats.submissions) == 1
        assert ats.posts == [ats.apply_path]
        # O desafio foi resolvido ANTES da escrita: o marcador saiu da página.
        assert harness.marker_present() is False
        assert harness.application().context["journey"]["answered"] >= 3


def test_a_challenge_nobody_resolves_produces_zero_writes(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=True,
            captcha_wait=0.2,
            poll_seconds=0.05,
            on_wait=None,  # ninguém resolve
        )

        result = harness.run()

        assert result.status == "NEEDS_CAPTCHA"
        assert result.state is ApplicationState.NEEDS_CAPTCHA
        assert result.submission_attempted is False
        assert result.submission_writes == 0
        # Nada saiu: nem POST, nem tentativa registrada, nem intent armada.
        assert ats.posts == []
        assert ats.submissions == []
        assert harness.writes() == 0
        assert harness.database.list_submission_attempts(harness.application().id) == []
        # A prova durável de que HAVIA desafio e que ele nao foi resolvido: o
        # journal da Application (a sessao de browser ja foi fechada).
        events = {event.event: event.payload for event in harness.database.list_application_events(harness.application().id)}
        blocked = events["challenge_detected_before_submit"]
        assert blocked["decision"] == "needs_human"
        assert blocked["blocking"] is True
        assert blocked["provider"] == "recaptcha"
        assert blocked["rounds"] >= 1
        assert blocked["waited_seconds"] >= 0.2


def test_the_blocked_state_is_resumable_not_terminal(tmp_path: Path):
    """`NEEDS_CAPTCHA` (não `NEEDS_HUMAN_CAPTCHA`): nada saiu e a etapa retoma."""
    from jobsearch_agent.application import TRANSITIONS

    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)
        result = harness.run()

        assert result.terminal is False
        assert result.requires_action == ApplicationState.NEEDS_CAPTCHA.value
        assert ApplicationState.PREPARING in TRANSITIONS[ApplicationState.NEEDS_CAPTCHA]
