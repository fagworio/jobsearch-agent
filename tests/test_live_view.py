"""Relay do operador: as três travas que impedem o operador de submeter.

Estes testes fixam o invariante que a Fase 3 do plano declarava como o mais
importante — "o operador nunca tem acesso ao budget de submissão":

  - o submit fica desabilitado durante a janela do operador, e é restaurado
    depois, sem tocar em controles que já estavam desabilitados;
  - um clique que cai sobre um controle de submit é recusado (defesa em
    profundidade);
  - `Enter`, `Space` e `Tab` são recusados: num formulário HTML eles acionam
    (ou levam o foco até) o botão de envio;
  - sem handoff aberto, nenhum input é aceito;
  - comandos vêm de qualquer thread, mas só executam na thread da página.

Nenhum teste aqui abre browser: o relay é testado contra uma página falsa que
registra o que foi pedido.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from jobsearch_agent.live_view import (
    ALLOWED_KEYS,
    SUBMIT_CONTROL_SELECTOR,
    SUBMIT_TRIGGER_KEYS,
    LiveViewRelay,
    OperatorCommand,
)


@dataclass
class _FakePage:
    """Página falsa: registra cliques, teclas, texto e chamadas de script."""

    hit_submit: bool = False
    hit_nothing: bool = False
    fail_screenshot: bool = False
    clicks: list[tuple[int, int]] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)
    evaluated: list[tuple[str, Any]] = field(default_factory=list)
    locked: int = 2
    restored: int = 2

    class mouse:  # noqa: N801 - espelha a API do Playwright
        def __init__(self, page: "_FakePage") -> None:
            self._page = page

        def click(self, x: int, y: int) -> None:
            self._page.clicks.append((x, y))

    class keyboard:  # noqa: N801 - espelha a API do Playwright
        def __init__(self, page: "_FakePage") -> None:
            self._page = page

        def press(self, key: str) -> None:
            self._page.keys.append(key)

        def type(self, text: str) -> None:
            self._page.typed.append(text)

    def __post_init__(self) -> None:
        self.mouse = _FakePage.mouse(self)
        self.keyboard = _FakePage.keyboard(self)

    def evaluate(self, script: str, argument: Any = None) -> Any:
        self.evaluated.append((script, argument))
        if "elementFromPoint" in script:
            if self.hit_nothing:
                return "no_element"
            return "submit_control" if self.hit_submit else "ok"
        # O script de lock também menciona `data-liveview-locked`; o que
        # distingue os dois é a direção da atribuição.
        if "disabled = false" in script:
            return self.restored
        if "disabled = true" in script:
            return self.locked
        return None

    def screenshot(self, **_kwargs: Any) -> bytes:
        if self.fail_screenshot:
            raise RuntimeError("browser fechado")
        return b"\x89PNG-fake"


def test_keys_that_can_submit_are_not_in_the_allowlist():
    """`Enter`/`Space`/`Tab` fora da allowlist — e a allowlist não os contém."""
    assert {"Enter", "Space", "Tab"} <= SUBMIT_TRIGGER_KEYS
    assert SUBMIT_TRIGGER_KEYS.isdisjoint(ALLOWED_KEYS)


def test_a_command_queue_can_be_filled_from_another_thread():
    relay = LiveViewRelay()
    page = _FakePage()

    worker = threading.Thread(target=lambda: [relay.offer(OperatorCommand("screenshot")) for _ in range(3)])
    worker.start()
    worker.join()

    assert relay.pending == 3
    results = relay.drain(page)
    assert [result.kind for result in results] == ["screenshot"] * 3
    assert all(result.accepted for result in results)
    assert relay.last_screenshot.startswith(b"\x89PNG")


def test_drain_refuses_to_run_outside_the_page_thread():
    """A API síncrona do Playwright é presa à thread: o relay também é."""
    relay = LiveViewRelay()
    failure: list[str] = []

    def attempt() -> None:
        try:
            relay.drain(_FakePage())
        except RuntimeError as exc:
            failure.append(str(exc))

    worker = threading.Thread(target=attempt)
    worker.start()
    worker.join()

    assert failure and "thread" in failure[0]


def test_input_is_refused_when_no_handoff_is_open():
    relay = LiveViewRelay()
    page = _FakePage()

    relay.offer(OperatorCommand("click", x=10, y=10))
    relay.offer(OperatorCommand("type", text="abc"))
    relay.offer(OperatorCommand("press", key="Backspace"))
    results = relay.drain(page)

    assert [result.reason for result in results] == ["no_handoff_open"] * 3
    assert page.clicks == [] and page.typed == [] and page.keys == []


def test_the_submit_control_is_disabled_while_the_window_is_open():
    relay = LiveViewRelay()
    page = _FakePage()

    changed = relay.lock_submit(page)
    assert changed == 2
    assert relay.submit_locked is True
    # O seletor vai como ARGUMENTO do script, não embutido nele.
    assert any(argument == SUBMIT_CONTROL_SELECTOR for _script, argument in page.evaluated)

    restored = relay.restore_submit(page)
    assert restored == 2
    assert relay.submit_locked is False


def test_the_restore_only_touches_what_we_locked():
    """Idempotência: um controle desabilitado pela própria página não é reabilitado."""
    relay = LiveViewRelay()
    page = _FakePage()

    relay.lock_submit(page)
    relay.lock_submit(page)  # idempotente
    relay.restore_submit(page)

    scripts = [script for script, _arg in page.evaluated]
    lock_scripts = [script for script in scripts if "disabled = true" in script]
    unlock_scripts = [script for script in scripts if "disabled = false" in script]
    # Dois locks (idempotente no estado, mas o script roda), UM restore.
    assert len(lock_scripts) == 2
    assert len(unlock_scripts) == 1


@pytest.mark.parametrize("key", sorted(SUBMIT_TRIGGER_KEYS))
def test_keys_that_can_submit_are_refused(key: str):
    relay = LiveViewRelay()
    page = _FakePage()
    relay.lock_submit(page)

    relay.offer(OperatorCommand("press", key=key))
    result = relay.drain(page)[0]

    assert result.accepted is False
    assert result.reason == "key_can_submit"
    assert page.keys == []


def test_a_click_on_the_submit_control_is_refused():
    relay = LiveViewRelay()
    page = _FakePage(hit_submit=True)
    relay.lock_submit(page)

    relay.offer(OperatorCommand("click", x=300, y=700))
    result = relay.drain(page)[0]

    assert result.accepted is False
    assert result.reason == "click_on_submit_control"
    assert page.clicks == []


def test_a_click_that_hits_nothing_is_refused():
    """Fora da viewport não há clique: aceitar seria relay de ruído."""
    relay = LiveViewRelay()
    page = _FakePage(hit_nothing=True)
    relay.lock_submit(page)

    relay.offer(OperatorCommand("click", x=400, y=9000))
    result = relay.drain(page)[0]

    assert result.accepted is False
    assert result.reason == "click_on_no_element"
    assert page.clicks == []


def test_a_click_anywhere_else_is_relayed():
    relay = LiveViewRelay()
    page = _FakePage(hit_submit=False)
    relay.lock_submit(page)

    relay.offer(OperatorCommand("click", x=120, y=280))
    result = relay.drain(page)[0]

    assert result.accepted is True
    assert page.clicks == [(120, 280)]


def test_allowed_keys_and_typing_are_relayed():
    relay = LiveViewRelay()
    page = _FakePage()
    relay.lock_submit(page)

    relay.offer(OperatorCommand("press", key="Backspace"))
    relay.offer(OperatorCommand("type", text="AB12"))
    results = relay.drain(page)

    assert [result.accepted for result in results] == [True, True]
    assert page.keys == ["Backspace"]
    assert page.typed == ["AB12"]


def test_absurdly_long_text_is_refused():
    relay = LiveViewRelay()
    page = _FakePage()
    relay.lock_submit(page)

    relay.offer(OperatorCommand("type", text="x" * 500))
    result = relay.drain(page)[0]

    assert result.accepted is False
    assert result.reason == "text_too_long"
    assert page.typed == []


def test_an_unknown_command_is_refused_not_ignored():
    relay = LiveViewRelay()
    page = _FakePage()
    relay.lock_submit(page)

    relay.offer(OperatorCommand("navigate", text="https://example.invalid"))
    result = relay.drain(page)[0]

    assert result.accepted is False
    assert result.reason == "unknown_command"


def test_a_full_queue_refuses_instead_of_slowing_the_loop():
    relay = LiveViewRelay(max_pending=2)

    assert relay.offer(OperatorCommand("screenshot")) is True
    assert relay.offer(OperatorCommand("screenshot")) is True
    assert relay.offer(OperatorCommand("screenshot")) is False
    assert relay.results[-1].reason == "queue_full"


def test_a_broken_screenshot_is_reported_and_does_not_raise():
    relay = LiveViewRelay()
    page = _FakePage(fail_screenshot=True)
    relay.offer(OperatorCommand("screenshot"))

    result = relay.drain(page)[0]

    assert result.accepted is False
    assert result.reason == "screenshot_failed:RuntimeError"


def test_the_journal_projection_never_carries_what_was_typed():
    relay = LiveViewRelay()
    page = _FakePage()
    relay.lock_submit(page)
    relay.offer(OperatorCommand("type", text="segredo-do-operador"))
    relay.offer(OperatorCommand("press", key="Enter"))
    relay.drain(page)

    projection = relay.as_journal()

    assert projection["commands"] == 2
    assert projection["rejected"] == 1
    assert projection["rejection_reasons"] == ["key_can_submit"]
    assert "segredo-do-operador" not in str(projection)
