"""Gate de challenge ANTES da escrita: só observa, nunca resolve.

O `challenge-guard` responde três perguntas com o que já sabe fazer:

    needs_human         há um desafio que exige uma pessoa
    resolved_externally a pessoa resolveu (o desafio sumiu da página)
    provider_rejected   o provedor recusou uma candidatura que saiu

Este módulo usa as duas primeiras **antes** de o agente autorizar qualquer
escrita. Ele não clica, não digita, não copia token e não injeta nada: quem
interage com o desafio é a pessoa, na janela visível. O que o gate faz é
perguntar, esperar dentro de um orçamento e dizer ao loop se pode seguir.

Por que isso é seguro:

- roda antes de existir intent/autorização, então não há orçamento de escrita
  vivo durante a espera;
- a única chamada que faz no browser é a observação do próprio guard;
- o pior caso é bloquear: `NEEDS_CAPTCHA` com zero escritas, que é retomável.

O gate é **opt-in**: `ENABLE_CHALLENGE_RESOLUTION=false` (o padrão) deixa
`LoopRuntime.challenge_gate` como `None` e o loop segue exatamente como antes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .challenges import ChallengeOutcome, JobsearchChallengeAdapter

#: Decisões do guard que significam "pode seguir": não há desafio, ou o desafio
#: que havia foi resolvido externamente.
PROCEED_DECISIONS: frozenset[str] = frozenset({"none", "resolved_externally", "unknown"})


@dataclass(frozen=True)
class ChallengeGateResult:
    """O que o gate observou, em forma auditável e sem segredo."""

    blocking: bool
    resolved: bool
    decision: str
    provider: str
    rounds: int
    waited_seconds: float
    session_id: str = ""

    def as_journal(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "resolved": self.resolved,
            "decision": self.decision,
            "provider": self.provider,
            "rounds": self.rounds,
            "waited_seconds": round(self.waited_seconds, 3),
            "session_id": self.session_id,
        }


class PreSubmitChallengeGate:
    """Observa o estado anti-bot antes de autorizar a escrita.

    `wait_seconds` é o orçamento para uma pessoa resolver na janela visível. Com
    zero (o padrão) o gate é uma única observação — e um desafio presente já
    bloqueia, que é o comportamento conservador.
    """

    def __init__(
        self,
        *,
        adapter_factory: Callable[[], JobsearchChallengeAdapter] = JobsearchChallengeAdapter,
        poll_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._poll_seconds = max(poll_seconds, 0.05)
        self._sleep = sleep
        self._clock = clock

    def evaluate(self, page: Any, *, wait_seconds: float = 0.0) -> ChallengeGateResult:
        """Uma leitura quando não há espera; observação repetida quando há.

        Nunca levanta por falha do guard: um erro de observação não pode virar
        escrita liberada. Falha de observação = bloqueio, com o motivo gravado.
        """
        adapter = self._adapter_factory()
        started = self._clock()
        rounds = 0
        resolved = False
        outcome: ChallengeOutcome | None = None
        try:
            adapter.attach(page)
            while True:
                rounds += 1
                try:
                    outcome = adapter.observe(step="pre_submit")
                except Exception as exc:  # observação quebrada nunca libera escrita
                    return ChallengeGateResult(
                        blocking=True,
                        resolved=False,
                        decision=f"observation_failed:{type(exc).__name__}",
                        provider="",
                        rounds=rounds,
                        waited_seconds=self._clock() - started,
                    )
                decision = str(getattr(outcome, "decision", "") or "")
                if decision in PROCEED_DECISIONS:
                    return ChallengeGateResult(
                        blocking=False,
                        resolved=bool(resolved or decision == "resolved_externally"),
                        decision=decision,
                        provider=str(getattr(outcome, "provider", "") or ""),
                        rounds=rounds,
                        waited_seconds=self._clock() - started,
                        session_id=str(getattr(outcome, "session_id", "") or ""),
                    )
                # Desafio presente: a partir daqui, tudo o que o gate faz é
                # esperar e reobservar dentro do orçamento.
                resolved = True
                if self._clock() - started >= max(wait_seconds, 0.0):
                    return ChallengeGateResult(
                        blocking=True,
                        resolved=True,
                        decision=decision,
                        provider=str(getattr(outcome, "provider", "") or ""),
                        rounds=rounds,
                        waited_seconds=self._clock() - started,
                        session_id=str(getattr(outcome, "session_id", "") or ""),
                    )
                self._sleep(self._poll_seconds)
        finally:
            try:
                adapter.detach()
            except Exception:  # pragma: no cover - desanexar nunca derruba o loop
                pass


__all__ = ["ChallengeGateResult", "PreSubmitChallengeGate", "PROCEED_DECISIONS"]
