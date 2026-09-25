"""CG-036 — a ACL entre o runtime publico do guard e o dominio do host.

Antes de 0.2.0 o host montava a composicao inteira por conta propria: observer,
engine, validador, limites e policy. Cada uma dessas pecas carregava uma copia da
mesma decisao, e a divergencia so aparecia quando um caminho esquecia uma regra.

Depois de 0.2.0 o guard entrega **um** resultado (`ChallengeRuntimeResult`, com
conjunto fechado de campos) e o host o traduz **uma vez**, aqui:

```text
ChallengeRuntimeResult  ->  ChallengeAclOutcome  ->  ApplicationState
```

Duas regras que a ACL nao negocia:

1. **Nao inventa estado forte.** `unknown` fica sem estado: a Application
   permanece onde estava, e o host decide com informacao, nao com palpite.
2. **Escrever nao e resolver.** `provider_rejected` com escrita e handoff
   (`NEEDS_HUMAN_CAPTCHA`); sem escrita e retomavel (`NEEDS_CAPTCHA`) — a
   diferenca e o que aconteceu com a candidatura, nao o veredito do desafio.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from challenge_guard import ChallengeRuntime, ChallengeRuntimeResult, RuntimeLimits

from .models import ApplicationState


class ChallengeAclHandling(str, Enum):
    """O que o loop faz com o resultado do runtime."""

    CONTINUE = "continue"                    # sem desafio no caminho: seguir
    NEEDS_HUMAN = "needs_human"              # parar, retomavel
    PROVIDER_REJECTED = "provider_rejected"  # escrita recusada: handoff
    UNKNOWN = "unknown"                      # sem estado forte
    NOT_DETECTED = "not_detected"            # nao havia desafio


@dataclass(frozen=True)
class ChallengeAclOutcome:
    """Traducao completa de um ciclo do runtime para o dominio do host."""

    handling: ChallengeAclHandling
    final_status: str
    reason_token: str
    state: ApplicationState | None = None
    human_required: bool = False
    expired: bool = False
    detected: bool = False
    session_id: str = ""
    provider: str = ""
    challenge_type: str = ""
    rounds: int = 0
    confidence: float = 0.0
    capability: str = ""
    backend: str = ""
    reason: str = ""

    @property
    def blocks(self) -> bool:
        """O loop NAO pode seguir para a escrita."""
        return self.handling in {
            ChallengeAclHandling.NEEDS_HUMAN,
            ChallengeAclHandling.PROVIDER_REJECTED,
            ChallengeAclHandling.UNKNOWN,
        }

    @property
    def waitable(self) -> bool:
        """Vale a pena continuar reobservando a janela do operador.

        Diferente de `blocks`: uma rejeicao do provedor nao fica melhor com mais
        rodadas, e `unknown` nao melhora com palpite. So `needs_human` (e que
        ainda nao expirou) justifica esperar.
        """
        return self.handling is ChallengeAclHandling.NEEDS_HUMAN and not self.expired

    @property
    def resolved(self) -> bool:
        return self.handling is ChallengeAclHandling.CONTINUE

    def provenance(self) -> dict[str, object]:
        """Projecao SEGURA para o journal: conjunto fechado, sem URL nem segredo."""
        return {
            "provider": self.provider,
            "reason_token": self.reason_token,
            "session_id": self.session_id,
            "decision": self.final_status,
            "rounds": self.rounds,
            "confidence": round(float(self.confidence), 3),
            "capability": self.capability,
        }

    def as_journal(self) -> dict[str, object]:
        """O que o loop grava. `final_status` e o vocabulario do guard."""
        return {
            "handling": self.handling.value,
            "resolved": self.resolved,
            "rounds": self.rounds,
            "final_status": self.final_status,
            "reason_token": self.reason_token,
            "provider": self.provider,
            "challenge_type": self.challenge_type,
            "session_id": self.session_id,
            "human_required": self.human_required,
            "expired": self.expired,
            "capability": self.capability,
            "backend": self.backend,
            "reason": self.reason,
        }


def map_runtime_result(result: ChallengeRuntimeResult, *, browser_write_sent: bool = False) -> ChallengeAclOutcome:
    """Traduz o resultado do guard. Tabela fecha, sem heuristica.

    ```text
    none / observe (sem deteccao)   -> NOT_DETECTED  (nada a fazer)
    resolved_externally             -> CONTINUE      (o desafio saiu do caminho)
    human_required                  -> NEEDS_HUMAN   -> NEEDS_CAPTCHA
    expired                         -> NEEDS_HUMAN   -> NEEDS_CAPTCHA (retomavel)
    provider_rejected + escrita     -> PROVIDER_REJECTED -> NEEDS_HUMAN_CAPTCHA
    provider_rejected sem escrita   -> NEEDS_HUMAN       -> NEEDS_CAPTCHA
    unknown                         -> UNKNOWN        (sem estado)
    observe COM deteccao            -> NEEDS_HUMAN   -> NEEDS_CAPTCHA (ver nota)
    ```

    A nota do ultimo caso importa: o guard diz "continue observando" para um
    desafio nao interativo, e o host concorda — mas nao com uma escrita no meio.
    Como o host nao tem como resolver o desafio, ele para de forma retomavel em
    vez de submeter com um widget na tela.
    """
    detected = result.detected
    status = result.final_status
    base = dict(
        final_status=status,
        reason_token=result.reason_token,
        human_required=result.human_required,
        detected=detected,
        session_id=result.session_id,
        provider=result.provider,
        challenge_type=result.challenge_type,
        rounds=result.rounds,
        confidence=result.confidence,
        capability=result.capability,
        backend=result.backend,
    )

    if not detected and status in {"none", "observe"}:
        return ChallengeAclOutcome(ChallengeAclHandling.NOT_DETECTED, reason="no challenge observed", **base)
    if status == "resolved_externally":
        return ChallengeAclOutcome(ChallengeAclHandling.CONTINUE, **base)
    if status == "provider_rejected":
        if browser_write_sent:
            return ChallengeAclOutcome(
                ChallengeAclHandling.PROVIDER_REJECTED,
                state=ApplicationState.NEEDS_HUMAN_CAPTCHA,
                reason="provider rejected a submission that was written",
                **base,
            )
        return ChallengeAclOutcome(
            ChallengeAclHandling.NEEDS_HUMAN,
            state=ApplicationState.NEEDS_CAPTCHA,
            reason="provider rejection without a write: nothing was sent",
            **base,
        )
    if status == "expired":
        return ChallengeAclOutcome(
            ChallengeAclHandling.NEEDS_HUMAN,
            state=ApplicationState.NEEDS_CAPTCHA,
            expired=True,
            reason="observation budget expired without an outcome",
            **base,
        )
    if status == "human_required":
        return ChallengeAclOutcome(
            ChallengeAclHandling.NEEDS_HUMAN,
            state=ApplicationState.NEEDS_CAPTCHA,
            reason="challenge requires a human",
            **base,
        )
    if status == "observe":
        return ChallengeAclOutcome(
            ChallengeAclHandling.NEEDS_HUMAN,
            state=ApplicationState.NEEDS_CAPTCHA,
            reason="challenge still observed; the host does not write with it on screen",
            **base,
        )
    # `unknown`: nao ha estado forte a inventar.
    return ChallengeAclOutcome(ChallengeAclHandling.UNKNOWN, reason="runtime could not determine an outcome", **base)


@dataclass
class RuntimeBudget:
    """Orcamento traduzido do relogio do host para o runtime do guard."""

    wait_seconds: float = 0.0
    poll_seconds: float = 0.0

    def limits(self) -> RuntimeLimits:
        """Traduz o relogio do host em rounds.

        `wait_seconds <= 0` significa UMA leitura: e o que o host declara quando
        nao ha janela de operador (`--captcha-wait 0`). Sem essa regra, um poll
        curto transformaria "sem espera" em varias rodadas.
        """
        wait = max(float(self.wait_seconds), 0.1)
        if self.wait_seconds <= 0 or self.poll_seconds <= 0:
            rounds = 1
        else:
            rounds = max(1, int(float(self.wait_seconds) / max(float(self.poll_seconds), 0.01)))
        return RuntimeLimits(max_rounds=rounds, timeout_seconds=wait, max_duration_seconds=wait * 2)


def runtime_for_page(
    page: Any,
    *,
    wait_seconds: float = 0.0,
    poll_seconds: float = 0.0,
    adapter: Any | None = None,
) -> ChallengeRuntime:
    """Runtime do guard ligado a uma pagina viva, com o orcamento do host."""
    if adapter is None:
        from challenge_guard.browser import PlaywrightChallengeAdapter

        adapter = PlaywrightChallengeAdapter()
    return ChallengeRuntime(
        page=page,
        adapter=adapter,
        limits=RuntimeBudget(wait_seconds=wait_seconds, poll_seconds=poll_seconds).limits(),
    )


__all__ = [
    "ChallengeAclHandling",
    "ChallengeAclOutcome",
    "RuntimeBudget",
    "map_runtime_result",
    "runtime_for_page",
]
