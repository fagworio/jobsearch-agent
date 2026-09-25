"""O operador humano nunca alcança o controle de envio.

Esta é a prova central da Fase 3. Um "operador" de teste entrega comandos pela
fila do relay (de outro thread, como um transporte real faria) e o loop os
executa na thread da página. Três travas independentes são exercitadas contra o
Chromium real:

  1. o controle de envio fica **desabilitado** enquanto a janela está aberta;
  2. um clique que cai sobre ele é recusado;
  3. `Enter` (e qualquer tecla que submeta) é recusado.

O caso final é o que importa: um operador que só tenta sequestrar o envio
produz **zero POSTs**.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

from jobsearch_agent.live_view import OperatorCommand
from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import SUBMITTED, build_harness

pytestmark = pytest.mark.integration

def _center_of(page, selector: str) -> tuple[int, int]:
    """Centro real do elemento, medido NA THREAD DA PÁGINA.

    Um operador de verdade mira pelo que vê; um teste que chuta coordenadas fixas
    mede a própria suposição, não o produto.
    """
    box = page.evaluate(
        """(selector) => {
             const element = document.querySelector(selector);
             if (!element) return null;
             const rect = element.getBoundingClientRect();
             return {x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2)};
           }""",
        selector,
    )
    assert box, f"elemento não encontrado: {selector}"
    return int(box["x"]), int(box["y"])


def _click_on(page, selector: str) -> OperatorCommand:
    x, y = _center_of(page, selector)
    return OperatorCommand("click", x=x, y=y)


class _FakeOperator:
    """Entrega comandos UMA vez — como um transporte real faria.

    Cada comando é resolvido na thread da página (para medir coordenadas reais) e
    enfileirado no relay. É o mesmo caminho de um operador remoto: `offer()` de
    fora, execução na thread que possui o browser.
    """

    def __init__(self, plan: list) -> None:
        self._plan = list(plan)
        self.sent = False
        self.relay = None

    def __call__(self, page, observed: list) -> None:
        if self.sent:
            return
        self.sent = True
        assert self.relay is not None
        for step in self._plan:
            command = step(page) if callable(step) else step
            self.relay.offer(command)
        observed.append({
            "locked": page.evaluate("() => document.querySelectorAll('[data-liveview-locked]').length"),
            "submit_disabled": page.evaluate(
                "() => { const b = document.querySelector('button[type=submit]'); return b ? b.disabled : null; }"
            ),
        })


def test_the_submit_is_locked_during_the_window_and_restored_afterwards(tmp_path: Path):
    """A trava vale durante a janela e não vaza para depois dela."""
    captured: list = []

    def look_only(page, observed: list) -> None:
        observed.append({
            "locked": page.evaluate("() => document.querySelectorAll('[data-liveview-locked]').length"),
            "disabled": page.evaluate("() => { const b = document.querySelector('button[type=submit]'); return b ? b.disabled : null; }"),
        })

    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path, ats, resolution_enabled=True, captcha_wait=0.3, poll_seconds=0.05, on_wait=look_only,
        )
        result = harness.run()

        # Ninguém resolveu: bloqueou.
        assert result.state is ApplicationState.NEEDS_CAPTCHA
        # Enquanto a janela estava aberta, o controle de envio estava desabilitado.
        assert harness.observed, "o hook de espera nunca rodou"
        assert all(entry["locked"] >= 1 for entry in harness.observed)
        assert all(entry["disabled"] is True for entry in harness.observed)
        # Fechada a janela, a trava foi removida (o relay restaurou).
        assert harness.relay is not None
        assert harness.relay.submit_locked is False
        assert ats.posts == []


def test_an_operator_that_only_tries_to_submit_produces_zero_writes(tmp_path: Path):
    operator = _FakeOperator(
        [
            # Mira o botão de envio DE VERDADE (medido na página).
            lambda page: _click_on(page, "button[type=submit]"),
            OperatorCommand("press", key="Enter"),
            OperatorCommand("press", key="Tab"),
            OperatorCommand("press", key="Space"),
        ]
    )
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path, ats, resolution_enabled=True, captcha_wait=0.4, poll_seconds=0.05,
            on_wait=operator,
        )
        operator.relay = harness.relay
        result = harness.run()

        # O desafio nunca foi resolvido, e NADA saiu.
        assert result.status == "NEEDS_CAPTCHA"
        assert result.submission_writes == 0
        assert ats.posts == []
        assert harness.writes() == 0

        # Cada tentativa de sequestrar o envio foi recusada, com motivo.
        relay = harness.relay
        assert relay is not None
        reasons = [r.reason for r in relay.results if not r.accepted]
        assert "key_can_submit" in reasons
        # O clique mirado no botão foi recusado pelo hit-test, e nenhuma escrita
        # saiu — nem pelo clique, nem pelas teclas.
        assert "click_on_submit_control" in reasons
        assert ats.posts == []


def test_an_operator_that_solves_the_challenge_lets_the_single_write_through(tmp_path: Path):
    operator = _FakeOperator(
        [
            OperatorCommand("press", key="Enter"),                      # atalho: recusado
            lambda page: _click_on(page, "#gate-challenge"),            # o clique que resolve
        ]
    )
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path, ats, resolution_enabled=True, captcha_wait=3.0, poll_seconds=0.05,
            on_wait=operator,
        )
        operator.relay = harness.relay
        result = harness.run()

        assert result.status == SUBMITTED, (result.status, result.reason, result.to_dict())
        assert result.state is ApplicationState.SUBMITTED
        # Exatamente um POST — o da candidatura, depois do desafio resolvido.
        assert len(ats.posts) == 1
        assert harness.writes() == 1
        relay = harness.relay
        assert relay is not None
        assert any(r.accepted for r in relay.results if r.kind == "click")
        assert not relay.submit_locked  # janela fechada, submit restaurado


def test_a_click_on_the_submit_control_is_refused_with_the_reason_recorded(tmp_path: Path):
    """O motivo fica registrado — auditar não depende de reproduzir a sessão."""
    operator = _FakeOperator([lambda page: _click_on(page, "button[type=submit]")])
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(
            tmp_path, ats, resolution_enabled=True, captcha_wait=0.3, poll_seconds=0.05,
            on_wait=operator,
        )
        operator.relay = harness.relay
        harness.run()

        relay = harness.relay
        assert relay is not None
        clicks = [r for r in relay.results if r.kind == "click"]
        assert clicks and all(not r.accepted for r in clicks)
        assert clicks[0].reason == "click_on_submit_control"
        assert ats.posts == []
