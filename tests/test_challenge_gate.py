"""Gate de challenge antes da escrita: unidade, com guard roteirizado.

O gate é a única parte do produto que pergunta ao `challenge-guard` se existe um
desafio **antes** de autorizar a escrita. Estes testes fixam o que ele pode e o
que ele não pode fazer:

  - desafio presente e resolvido dentro do orçamento → segue;
  - desafio presente e não resolvido → bloqueia (e o loop não escreve);
  - sem desafio → segue, sem esperar;
  - observação quebrada → bloqueia (nunca libera escrita);
  - ele NÃO interage: a única chamada que chega ao adapter é `observe`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jobsearch_agent.challenge_gate import PreSubmitChallengeGate


@dataclass
class _Outcome:
    decision: str
    provider: str = "recaptcha"
    session_id: str = "challenge-session-0001"
    human_required: bool = False


class _ScriptedAdapter:
    """Adapter roteirizado: devolve a próxima decisão a cada observação."""

    def __init__(self, decisions: list[str]) -> None:
        self._decisions = list(decisions)
        self.observations: list[str] = []
        self.attached: list[Any] = []
        self.detached = 0
        self.last_decision = "none"

    def attach(self, page: Any) -> None:
        self.attached.append(page)

    def detach(self) -> None:
        self.detached += 1

    def observe(self, *, step: str = "pre_submit") -> _Outcome:
        self.observations.append(step)
        self.last_decision = self._decisions.pop(0) if self._decisions else "none"
        return _Outcome(decision=self.last_decision, human_required=self.last_decision == "needs_human")


class _ExplodingAdapter(_ScriptedAdapter):
    def observe(self, *, step: str = "pre_submit") -> _Outcome:
        self.observations.append(step)
        raise RuntimeError("observer indisponivel")


@dataclass
class _FakeClock:
    """Relógio determinístico: só anda quando o gate dorme."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _gate(decisions: list[str], clock: _FakeClock) -> tuple[PreSubmitChallengeGate, _ScriptedAdapter]:
    adapter = _ScriptedAdapter(decisions)
    gate = PreSubmitChallengeGate(
        adapter_factory=lambda: adapter,
        poll_seconds=1.0,
        sleep=clock.sleep,
        clock=clock.monotonic,
    )
    return gate, adapter


def test_without_a_challenge_it_proceeds_without_waiting() -> None:
    clock = _FakeClock()
    gate, adapter = _gate(["none"], clock)

    result = gate.evaluate(object(), wait_seconds=0.0)

    assert result.blocking is False
    assert result.resolved is False
    assert result.rounds == 1
    assert clock.sleeps == []
    assert adapter.detached == 1


def test_a_challenge_without_budget_blocks_immediately() -> None:
    clock = _FakeClock()
    gate, adapter = _gate(["needs_human"], clock)

    result = gate.evaluate(object(), wait_seconds=0.0)

    assert result.blocking is True
    assert result.decision == "needs_human"
    assert result.rounds == 1
    assert clock.sleeps == []
    assert result.session_id == "challenge-session-0001"


def test_a_human_resolving_inside_the_budget_lets_the_loop_continue() -> None:
    """É isto que uma pessoa clicando na janela visível produz: o guard passa a
    reportar `resolved_externally` e o gate libera a escrita."""
    clock = _FakeClock()
    gate, adapter = _gate(["needs_human", "needs_human", "resolved_externally"], clock)

    result = gate.evaluate(object(), wait_seconds=5.0)

    assert result.blocking is False
    assert result.resolved is True
    assert result.rounds == 3
    assert clock.sleeps == [1.0, 1.0]
    assert result.waited_seconds == 2.0


def test_a_challenge_that_never_resolves_blocks_when_the_budget_runs_out() -> None:
    clock = _FakeClock()
    gate, adapter = _gate(["needs_human"] * 10, clock)

    result = gate.evaluate(object(), wait_seconds=3.0)

    assert result.blocking is True
    assert result.resolved is True  # apareceu desafio, e ele nao passou
    assert result.decision == "needs_human"
    assert clock.sleeps == [1.0, 1.0, 1.0]
    assert adapter.detached == 1


def test_pre_submit_is_the_phase_it_observes() -> None:
    """A fase é a que a escrita vai acontecer — nunca `post_submit`."""
    clock = _FakeClock()
    gate, adapter = _gate(["none"], clock)

    gate.evaluate(object(), wait_seconds=0.0)

    assert adapter.observations == ["pre_submit"]


def test_a_broken_observer_blocks_instead_of_releasing_the_write() -> None:
    clock = _FakeClock()
    adapter = _ExplodingAdapter([])
    gate = PreSubmitChallengeGate(
        adapter_factory=lambda: adapter,
        poll_seconds=1.0,
        sleep=clock.sleep,
        clock=clock.monotonic,
    )

    result = gate.evaluate(object(), wait_seconds=30.0)

    assert result.blocking is True
    assert result.decision.startswith("observation_failed:")
    assert adapter.detached == 1
    # Nem tentou esperar: falha de observacao e bloqueio imediato.
    assert clock.sleeps == []


def test_the_gate_never_touches_the_challenge() -> None:
    """A única superfície que o gate usa é `attach`, `observe` e `detach`.

    Não existe clique, digitação, cópia de token ou injeção: quem resolve é a
    pessoa. Este teste é a garantia de que o gate não ganha "esperteza" sem
    revisão.
    """
    clock = _FakeClock()
    gate, adapter = _gate(["needs_human", "resolved_externally"], clock)

    gate.evaluate(object(), wait_seconds=5.0)

    assert adapter.attached and adapter.detached == 1
    assert adapter.observations == ["pre_submit", "pre_submit"]
    public = {name for name in dir(adapter) if not name.startswith("_")}
    assert public == {"attach", "detach", "observe", "observations", "attached", "detached", "last_decision"}


def test_the_journal_projection_carries_no_secret() -> None:
    clock = _FakeClock()
    gate, _adapter = _gate(["needs_human"], clock)

    projection = gate.evaluate(object(), wait_seconds=0.0).as_journal()

    assert set(projection) == {
        "blocking",
        "resolved",
        "decision",
        "provider",
        "rounds",
        "waited_seconds",
        "session_id",
    }
