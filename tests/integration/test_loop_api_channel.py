"""O loop entregando a escrita ao canal de API — sem tocar em provider real.

O que este cenario prova, e por que ele nao e decorativo:

    o roteamento e o de PRODUCAO (`SubmissionRouter.channel_for` sobre a
    `DomainPolicy`), com a tabela de dominios declarada pelo teste;
    a credencial e exigida e conferida contra o destino;
    o browser preenche e sobe o curriculo, e NAO faz o POST da candidatura;
    a escrita sai UMA vez, pelo transporte de API, com o guard proprio;
    a contabilidade (intent autorizada, tentativa persistida, estado) e a mesma
    do canal de browser, porque a porta do dominio e a mesma.

O transporte e um fake e o destino e o loopback do ATS controlado: nenhum
provider e exercitado. Um sistema que diz "canal de API certificado" a partir
deste teste estaria mentindo — o que ele certifica e o ROTEAMENTO e o GUARD.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import SUBMITTED, build_harness

pytestmark = pytest.mark.integration


def test_the_loop_routes_the_write_to_the_api_channel_and_the_browser_never_posts(tmp_path: Path):
    with ChallengeCapableATS() as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=False, api_channel=True)

        result = harness.run()

        assert result.status == SUBMITTED, (result.status, result.reason)
        assert result.state is ApplicationState.SUBMITTED
        # O POST da candidatura NAO saiu do browser: saiu do canal de API.
        assert ats.posts == []
        assert harness.writes() == 0
        assert len(harness.api_requests) == 1
        request = harness.api_requests[0]
        assert request.method == "POST"
        assert request.url.endswith(f"/jobs/{ats_job_id()}/apply")
        assert request.headers["Authorization"] == "Bearer integration-secret"
        assert request.fields, "o payload de API precisa carregar as respostas"
        assert list(request.files) == ["job_application[resume]"]
        # A contabilidade e a do dominio, pela mesma porta do canal de browser.
        application = harness.application()
        intents = harness.database.list_submission_intents(application.id)
        attempts = harness.database.list_submission_attempts(application.id)
        assert [intent.status for intent in intents] == ["SUBMITTED"]
        assert [attempt.status for attempt in attempts] == ["SUBMITTED"]
        assert attempts[0].evidence["confirmation_type"] == "provider_http_response"


def ats_job_id() -> str:
    from tests.integration.challenge_ats import JOB_ID

    return JOB_ID


def test_without_the_api_channel_the_same_scenario_still_uses_the_browser(tmp_path: Path):
    """Controle do teste acima: sem router, o POST sai do browser como sempre."""
    with ChallengeCapableATS() as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=False)

        result = harness.run()

        assert result.status == SUBMITTED
        assert len(ats.posts) == 1
        assert harness.api_requests == []
