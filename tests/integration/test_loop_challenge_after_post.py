"""Cenário canônico #2 — challenge DEPOIS do POST.

O board recebe a candidatura e responde com a página de recusa anti-bot. É
exatamente o que a vaga real da Fueled e a da CI&T produziram.

Esperado:

  - exatamente UM POST (o guard prova: orçamento consumido uma vez);
  - estado `NEEDS_HUMAN_CAPTCHA` — "saiu e o provedor não confirmou";
  - nenhum reenvio, nem no mesmo run nem numa execução posterior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

pytestmark = pytest.mark.integration

from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import build_harness


def test_a_rejected_submission_never_resubmits(tmp_path: Path):
    with ChallengeCapableATS(reject_after_write=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)

        result = harness.run()

        assert result.submission_attempted is True
        assert result.submission_writes == 1
        assert result.state is ApplicationState.NEEDS_HUMAN_CAPTCHA
        assert result.status == "NEEDS_HUMAN_CAPTCHA"
        assert result.requires_action == ApplicationState.NEEDS_HUMAN_CAPTCHA.value
        # O servidor recebeu UM POST — nem zero, nem dois.
        assert ats.posts == [ats.apply_path]
        assert len(ats.submissions) == 1
        # A evidência registrada diz que a submissão saiu e foi recusada.
        attempts = harness.database.list_submission_attempts(harness.application().id)
        assert len(attempts) == 1
        assert attempts[0].status == ApplicationState.NEEDS_HUMAN_CAPTCHA.value
        assert attempts[0].evidence["submit_write"] is True
        assert attempts[0].evidence["confirmed_submission"] is False
        assert attempts[0].evidence["reason_token"] == "captcha_verification_failed"


def test_a_second_run_does_not_open_the_browser_again(tmp_path: Path):
    with ChallengeCapableATS(reject_after_write=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)
        harness.run()
        sessions_after_first = len(harness.sessions)

        again = harness.run()

        assert again.status == "NO_RESEND"
        assert again.submission_attempted is False
        assert again.submission_writes == 0
        # Nenhuma sessão nova e nenhum POST novo.
        assert len(harness.sessions) == sessions_after_first
        assert ats.posts == [ats.apply_path]


def test_the_budget_is_spent_exactly_once_even_with_a_challenge_on_the_page(tmp_path: Path):
    """Desafio visível de novo na página de recusa não libera orçamento novo."""
    with ChallengeCapableATS(reject_after_write=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)

        harness.run()

        assert ats.posts == [ats.apply_path]
        assert harness.writes() == 1
        guard = harness.guards[-1]
        assert guard.authorized_writes_remaining == 0
