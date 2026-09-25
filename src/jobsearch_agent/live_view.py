"""Relay do operador humano durante um desafio — com o submit fora de alcance.

O gate pré-POST já espera uma pessoa resolver o desafio na janela visível. Este
módulo é o que permite que essa pessoa esteja **em outro lugar**: as ações dela
entram por uma fila thread-safe e são executadas na thread que possui a sessão
do Playwright (a API síncrona é presa à thread — outro thread não pode tocar a
página).

A regra que governa tudo aqui:

    enquanto o handoff está aberto, o controle de envio está DESABILITADO por
    nós, e nenhuma ação do operador pode chegar nele.

São três travas independentes, cada uma testável:

1. **Lockdown**: ao abrir a janela do operador, todo controle de submit é
   desabilitado; ao fechar, é restaurado. Clicar num botão desabilitado não
   submete nada.
2. **Hit-test**: um clique do operador é recusado se cair sobre um controle de
   submit — defesa em profundidade para o caso de a página reabilitá-lo.
3. **Teclas**: `Enter`, `Space` e `Tab` são recusados. Num formulário HTML,
   Enter e Space acionam o botão de submit, e Tab leva o foco até ele. Nada
   disso é necessário para resolver um desafio (texto vai por `type_text`).

O que este módulo NÃO faz, por decisão de escopo: não navega, não executa
JavaScript arbitrário, não lê cookies/storage, não envia o formulário. Não há
endpoint — nem intenção — de submissão na superfície do operador.

Limite conhecido, declarado: a restrição por *bounding box* do desafio (clicar
apenas dentro do widget) depende de geometria que o `challenge-guard` v0.1.0 não
publica (`ChallengeObservation` traz só `challenge_dimensions`). Enquanto isso, a
garantia é o lockdown + o hit-test, que não dependem de conhecer o provider.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any

#: Teclas que um formulário pode transformar em envio. Recusadas.
SUBMIT_TRIGGER_KEYS: frozenset[str] = frozenset({"Enter", "Space", "Tab", "NumpadEnter"})

#: Teclas que o operador pode usar: edição e navegação dentro do widget.
ALLOWED_KEYS: frozenset[str] = frozenset(
    {"Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "Escape"}
)

#: Seletor dos controles finais. Sem vocabulário de anti-bot: é semântica de
#: formulário HTML.
SUBMIT_CONTROL_SELECTOR = (
    'button[type="submit"], input[type="submit"], button:not([type="button"]):not([type="reset"])'
)

#: Script que desabilita todo controle de submit e devolve quantos mudaram.
_LOCK_SCRIPT = """
    (selector) => {
      const controls = Array.from(document.querySelectorAll(selector));
      let changed = 0;
      for (const control of controls) {
        if (!control.disabled) {
          control.disabled = true;
          control.setAttribute('data-liveview-locked', '1');
          changed += 1;
        }
      }
      return changed;
    }
"""

#: Script que restaura apenas o que NÓS desabilitamos.
_UNLOCK_SCRIPT = """
    () => {
      const locked = Array.from(document.querySelectorAll('[data-liveview-locked="1"]'));
      let restored = 0;
      for (const control of locked) {
        control.disabled = false;
        control.removeAttribute('data-liveview-locked');
        restored += 1;
      }
      return restored;
    }
"""

#: Hit-test: "no_element" | "submit_control" | "ok".
#: Um ponto sem elemento (fora da viewport, por exemplo) não é um clique válido:
#: aceitar isso seria relay de ruído, e ruído não é auditável.
_HIT_TEST_SCRIPT = """
    ({x, y, selector}) => {
      const element = document.elementFromPoint(x, y);
      if (!element) return "no_element";
      return element.closest(selector) ? "submit_control" : "ok";
    }
"""


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    """Uma ação pedida pelo operador. Nenhum campo sensível."""

    kind: str  # "click" | "type" | "press" | "screenshot"
    x: int = 0
    y: int = 0
    text: str = ""
    key: str = ""


@dataclass(frozen=True, slots=True)
class OperatorCommandResult:
    """O que foi feito com o pedido, em forma auditável."""

    kind: str
    accepted: bool
    reason: str = ""

    def as_journal(self) -> dict[str, Any]:
        return {"kind": self.kind, "accepted": self.accepted, "reason": self.reason}


@dataclass
class LiveViewRelay:
    """Fila de comandos do operador, drenada na thread da sessão.

    `offer()` é seguro de qualquer thread (é por onde uma futura superfície
    HTTP/CLI entrega o comando). `drain()` só pode rodar na thread que criou o
    relay — a que possui a página.
    """

    max_pending: int = 32
    _commands: "queue.Queue[OperatorCommand]" = field(default_factory=queue.Queue)
    _owner_thread: int = field(default_factory=threading.get_ident)
    _pending: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    submit_locked: bool = False
    last_screenshot: bytes = b""
    results: list[OperatorCommandResult] = field(default_factory=list)

    # -- entrada ------------------------------------------------------------

    def offer(self, command: OperatorCommand) -> bool:
        """Enfileira um comando. Devolve `False` se a fila está cheia.

        Fila cheia é recusa, não espera: um operador não pode atrasar o loop, e
        comando descartado é comando que não aconteceu.
        """
        with self._lock:
            if self._pending >= self.max_pending:
                self.results.append(OperatorCommandResult(command.kind, False, "queue_full"))
                return False
            self._pending += 1
        self._commands.put(command)
        return True

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def lock_submit(self, page: Any) -> int:
        """Desabilita os controles de envio. Idempotente."""
        changed = int(page.evaluate(_LOCK_SCRIPT, SUBMIT_CONTROL_SELECTOR) or 0)
        self.submit_locked = True
        return changed

    def restore_submit(self, page: Any) -> int:
        """Restaura só o que nós desabilitamos. Idempotente."""
        restored = int(page.evaluate(_UNLOCK_SCRIPT) or 0)
        self.submit_locked = False
        return restored

    # -- execução (thread da sessão) ---------------------------------------

    def drain(self, page: Any) -> list[OperatorCommandResult]:
        """Executa o que houver na fila. Roda na thread que possui a página."""
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("LiveViewRelay.drain só pode rodar na thread que possui a página")
        executed: list[OperatorCommandResult] = []
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._pending = max(self._pending - 1, 0)
            result = self._execute(page, command)
            executed.append(result)
            self.results.append(result)
        return executed

    def _execute(self, page: Any, command: OperatorCommand) -> OperatorCommandResult:
        if command.kind == "screenshot":
            # Leitura pura: permitida em qualquer momento.
            try:
                self.last_screenshot = page.screenshot(type="png")
            except Exception as exc:  # pragma: no cover - defensivo
                return OperatorCommandResult("screenshot", False, f"screenshot_failed:{type(exc).__name__}")
            return OperatorCommandResult("screenshot", True)

        if not self.submit_locked:
            # Sem handoff aberto não existe janela de operador: nada de input.
            return OperatorCommandResult(command.kind, False, "no_handoff_open")

        if command.kind == "press":
            if command.key in SUBMIT_TRIGGER_KEYS:
                return OperatorCommandResult("press", False, "key_can_submit")
            if command.key not in ALLOWED_KEYS:
                return OperatorCommandResult("press", False, "key_not_allowed")
            page.keyboard.press(command.key)
            return OperatorCommandResult("press", True)

        if command.kind == "type":
            text = str(command.text)
            if len(text) > 256:
                return OperatorCommandResult("type", False, "text_too_long")
            page.keyboard.type(text)
            return OperatorCommandResult("type", True)

        if command.kind == "click":
            try:
                verdict = str(
                    page.evaluate(
                        _HIT_TEST_SCRIPT,
                        {"x": int(command.x), "y": int(command.y), "selector": SUBMIT_CONTROL_SELECTOR},
                    )
                    or ""
                )
            except Exception as exc:  # pragma: no cover - defensivo
                return OperatorCommandResult("click", False, f"hit_test_failed:{type(exc).__name__}")
            if verdict == "submit_control":
                return OperatorCommandResult("click", False, "click_on_submit_control")
            if verdict != "ok":
                return OperatorCommandResult("click", False, "click_on_no_element")
            page.mouse.click(int(command.x), int(command.y))
            return OperatorCommandResult("click", True)

        return OperatorCommandResult(command.kind, False, "unknown_command")

    # -- observabilidade ----------------------------------------------------

    def as_journal(self) -> dict[str, Any]:
        """Projeção segura: contagens e motivos, nunca o conteúdo digitado."""
        rejected = [result.reason for result in self.results if not result.accepted]
        return {
            "commands": len(self.results),
            "rejected": len(rejected),
            "rejection_reasons": sorted(set(rejected)),
            "submit_locked": self.submit_locked,
        }


__all__ = [
    "ALLOWED_KEYS",
    "LiveViewRelay",
    "OperatorCommand",
    "OperatorCommandResult",
    "SUBMIT_CONTROL_SELECTOR",
    "SUBMIT_TRIGGER_KEYS",
]
