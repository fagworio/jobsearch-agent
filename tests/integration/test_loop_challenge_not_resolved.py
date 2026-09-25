"""Cenário canônico #3 — challenge que não é resolvido.

O desafio está na página, ninguém resolve, e o orçamento acaba. O que se prova:

  - ZERO POSTs (do lado do servidor);
  - o guard nunca autorizou escrita (orçamento intacto, não só "não usado");
  - nenhuma tentativa registrada — não houve submissão a recusar;
  - estado retomável, com o motivo no journal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

pytestmark = pytest.mark.integration

from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import build_harness


def test_an_unresolved_challenge_never_reaches_the_network(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)

        result = harness.run()

        assert result.status == "NEEDS_CAPTCHA"
        assert result.state is ApplicationState.NEEDS_CAPTCHA
        assert ats.posts == []
        assert harness.writes() == 0
        # O orçamento nunca foi tocado: não houve autorização, não só "não uso".
        guard = harness.guards[-1]
        assert guard.authorized_writes_used == 0
        assert guard.blocked_writes == []


def test_an_unresolved_challenge_leaves_no_submission_attempt(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)

        result = harness.run()

        application = harness.application()
        assert harness.database.list_submission_attempts(application.id) == []
        assert harness.database.list_submission_intents(application.id) == []
        assert harness.database.get_review_snapshot(application.id) is None
        # O preenchimento continua registrado: nada foi perdido.
        assert result.unanswered_required == ()
        assert result.questions_answered >= 3


def test_the_journal_explains_why_it_stopped(tmp_path: Path):
    """"Por que falhou" se responde olhando o journal — e não o log do processo."""
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)

        harness.run()

        events = {event.event: event.payload for event in harness.database.list_application_events(harness.application().id)}
        assert "challenge_detected_before_submit" in events
        payload = events["challenge_detected_before_submit"]
        assert payload["decision"] == "needs_human"
        assert payload["provider"] == "recaptcha"
        assert payload["blocking"] is True
        assert payload["rounds"] >= 1
        # Nenhum campo de material sensível no journal.
        for key in payload:
            assert not any(part in key.casefold() for part in ("token", "cookie", "secret", "answer"))
