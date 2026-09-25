"""Feature flag desligada = comportamento legado, byte a byte.

`ENABLE_CHALLENGE_RESOLUTION=false` é o padrão. Nestes testes o gate nem existe
no runtime, e o loop se comporta exatamente como antes da evolução:

  - sem desafio: preenche e submete uma vez (SUBMITTED);
  - com desafio pré-POST: quem barra é o caminho de submit já existente, sem
    escrita, e sem nenhuma observação prévia do gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

pytestmark = pytest.mark.integration

from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import SUBMITTED, build_harness


def test_flag_off_keeps_the_happy_path(tmp_path: Path):
    with ChallengeCapableATS() as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=False)

        assert harness.gate is None
        result = harness.run()

        assert result.status == SUBMITTED
        assert harness.writes() == 1
        assert ats.posts == [ats.apply_path]


def test_flag_off_does_not_observe_before_the_write(tmp_path: Path):
    """Sem gate, o desafio só é descoberto no submit — e nada sai."""
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=False)

        result = harness.run()

        assert harness.gate is None
        assert result.submission_writes == 0
        assert ats.posts == []
        assert harness.writes() == 0
        # O estado vem do caminho de submit (desafio antes de qualquer escrita).
        assert result.state in {ApplicationState.NEEDS_CAPTCHA, ApplicationState.NEEDS_HUMAN_CAPTCHA}
        events = {event.event for event in harness.database.list_application_events(harness.application().id)}
        assert "challenge_detected_before_submit" not in events
