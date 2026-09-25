"""Ponte entre o `ApplicationLoop` e o subsistema de resolucao.

Este e o UNICO modulo do agente que conhece o pacote de resolucao, e ele existe
para que o loop nao precise conhecer: o loop ve `handle()` e uma decisao.

Invariantes que o modulo respeita:

- **Nunca escreve.** Nao existe POST aqui; a escrita continua sendo do
  `NetworkWriteGuard`, autorizada uma unica vez pelo loop.
- **Nunca reabre o formulario.** Se a resolucao invalidar o contrato, quem
  detecta e o loop (ele tem o fingerprint), e o resultado e parada — nao
  repreenchimento.
- **O submit fica fora do alcance do operador** durante a janela: o relay
  desabilita o controle de envio antes da orquestracao e restaura depois,
  inclusive quando a orquestracao levanta.
- **Nunca deixa excecao escapar**: falha de orquestracao vira parada.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .challenge_acl import (
    ChallengeAclHandling,
    ChallengeAclOutcome,
    map_runtime_result,
    runtime_for_page,
)

from .models import ApplicationState

from challenge_resolution.models import OrchestratorOutcome
from challenge_resolution.protocols import ChallengeObserver, ChallengeResolutionOrchestrator
from challenge_resolution.session import ChallengeSession
from challenge_resolution.types import ChallengeObservation, ChallengePhase

from .live_view import LiveViewRelay


#: Eventos duraveis da janela do operador.
#:
#: `started` sem `finished` e a assinatura de um processo que morreu no meio da
#: resolucao: a pessoa podia estar agindo, e o trabalho dela se perdeu junto com
#: o browser. Como o browser NAO volta, o que se pode fazer — e se deve — e
#: registrar o abandono em vez de fingir que a janela nunca existiu.
HANDOFF_STARTED = "challenge_handoff_started"
HANDOFF_FINISHED = "challenge_handoff_finished"
HANDOFF_ABANDONED = "challenge_handoff_abandoned"


class ChallengeHandling(str, Enum):
    """O que o loop deve fazer depois do tratamento."""

    CONTINUE = "continue"        # pode seguir para a escrita
    NEEDS_HUMAN = "needs_human"  # nao resolvido: parar (retomavel)
    NOT_DETECTED = "not_detected"  # nao havia desafio


@dataclass(frozen=True)
class ChallengeHandlingResult:
    handling: ChallengeHandling
    outcome: OrchestratorOutcome | None = None
    observation: ChallengeObservation | None = None
    #: Nome da excecao que interrompeu o ciclo, quando houve. Vazio = ciclo
    #: completo. Entra no journal para que "falhou" e "morreu" nao se confundam.
    failed_with: str = ""
    #: Caminho CG-036 (runtime publico do guard). Presente = a projecao do
    #: journal vem da ACL, que ja e um conjunto fechado de campos.
    acl: ChallengeAclOutcome | None = None

    @property
    def blocks(self) -> bool:
        return self.handling is ChallengeHandling.NEEDS_HUMAN

    def as_journal(self) -> dict[str, object]:
        """Projecao segura para o journal: sem segredo, sem material da pagina."""
        if self.acl is not None:
            payload = self.acl.as_journal()
            if self.failed_with:
                payload["failed_with"] = self.failed_with
            return payload
        if self.outcome is not None:
            return {
                "handling": self.handling.value,
                "resolved": self.outcome.resolved,
                "rounds": self.outcome.rounds,
                "final_status": self.outcome.final_status.value,
                "duration_seconds": round(self.outcome.duration_seconds, 3),
                "provider": self.outcome.provenance.provider,
                "challenge_type": self.outcome.provenance.challenge_type,
                "session_id": self.outcome.provenance.session_id,
            }
        payload = {
            "handling": self.handling.value,
            "resolved": False,
            "rounds": 0,
            "provider": getattr(self.observation, "provider", None).value if self.observation else "",
            "challenge_type": "",
            "session_id": "",
        }
        if self.failed_with:
            payload["failed_with"] = self.failed_with
        return payload


#: Como o observador e o orquestrador nascem a partir da PAGINA.
#:
#: A pagina so existe quando o loop abre a sessao, entao a composicao precisa ser
#: por fabrica: `observer_factory(page)` liga o guard a pagina viva, e
#: `orchestrator_factory(observer)` monta engine + validador + limites sobre esse
#: observador. Passar objetos prontos nao funcionaria — e foi o que a primeira
#: versao deste modulo fez de errado.
ObserverFactory = Callable[[Any], ChallengeObserver]
OrchestratorFactory = Callable[[ChallengeObserver], ChallengeResolutionOrchestrator]


#: Como o runtime publico nasce a partir da PAGINA (CG-036).
RuntimeFactory = Callable[[Any], Any]


@dataclass
class ChallengeIntegration:
    """Coordena o tratamento de um desafio dentro de uma execucao do loop."""

    #: Caminho 0.1.0 (observer + orquestrador do host). Opcional desde que exista
    #: `runtime_factory`: o host de 0.2.0 usa o runtime publico do guard.
    observer_factory: ObserverFactory | None = None
    orchestrator_factory: OrchestratorFactory | None = None
    relay: LiveViewRelay | None = None
    #: Desabilita o controle de envio durante a janela do operador.
    lock_submit: bool = True
    #: Banco para a evidencia DURAVEL da janela. Ausente = sem registro
    #: (testes de unidade da composicao); presente = `started`/`finished` e
    #: reconciliacao de janela abandonada por restart.
    database: Any | None = None
    #: Quanto se espera a pessoa, em segundos. Vai para a evidencia.
    wait_seconds: float = 0.0
    #: CG-036: quando presente, o guard entrega o ciclo (observacao, rounds,
    #: revalidacao, limites) pela API publica e a ACL traduz o resultado.
    runtime_factory: RuntimeFactory | None = None
    #: Espera entre rounds, na thread do loop. O RELOGIO e do host: e ele que
    #: sabe quanto tempo uma pessoa leva, e o guard so conta rounds e limites.
    sleep: Callable[[float], None] | None = None
    poll_seconds: float = 0.0

    def handle(self, page: Any, application_id: str) -> ChallengeHandlingResult:
        if page is None:
            return ChallengeHandlingResult(ChallengeHandling.NOT_DETECTED)
        if self.runtime_factory is not None:
            return self._handle_with_runtime(page, application_id)
        if self.observer_factory is None or self.orchestrator_factory is None:  # pragma: no cover - config
            raise ValueError("challenge integration requires observer/orchestrator factories or a runtime factory")
        return self._handle_with_orchestrator(page, application_id)

    # -- caminho 0.1.0 (observer + orquestrador do host) -----------------------

    def _handle_with_orchestrator(self, page: Any, application_id: str) -> ChallengeHandlingResult:
        """Uma saida so: fecha a janela SEMPRE, inclusive quando a orquestracao levanta.

        Antes, o `except` fazia `return` direto: a janela ficava com
        `HANDOFF_STARTED` e sem `HANDOFF_FINISHED`, e a execucao seguinte lia isso
        como "processo morreu no meio" — um processo que morreu de verdade e um
        que apenas falhou ficavam indistinguiveis.
        """
        observer = self.observer_factory(page)
        observation = observer.observe(phase=ChallengePhase.PRE_SUBMIT)
        if not observation.detected:
            _detach(observer)
            return ChallengeHandlingResult(ChallengeHandling.NOT_DETECTED, observation=observation)

        session = ChallengeSession.from_observation(observation, application_id=application_id)
        orchestrator = self.orchestrator_factory(observer)
        self._record(application_id, HANDOFF_STARTED, {
            "session_id": session.session_id,
            "provider": session.provider.value,
            "challenge_type": session.challenge_type,
            "wait_seconds": round(float(self.wait_seconds), 3),
        })
        locked = _lock(self.relay, page, self.lock_submit)
        failed = ""
        try:
            outcome = orchestrator.run(session)
        except Exception as exc:  # orquestrador promete nao levantar; se levantar, paramos
            failed = type(exc).__name__
            outcome = _failed_outcome(session)
        finally:
            _restore(self.relay, page, locked)
            _detach(observer)

        handling = ChallengeHandling.CONTINUE if outcome.resolved else ChallengeHandling.NEEDS_HUMAN
        result = ChallengeHandlingResult(handling, outcome=outcome, observation=observation, failed_with=failed)
        self._record(application_id, HANDOFF_FINISHED, dict(result.as_journal()))
        return result

    # -- caminho 0.2.0: o runtime publico do guard (CG-036) --------------------

    def _handle_with_runtime(self, page: Any, application_id: str) -> ChallengeHandlingResult:
        """O ciclo inteiro pelo runtime publico do guard (CG-036).

        O host continua dono de tres coisas que o guard nao tem como ter: a
        JANELA do operador (relay + trava de submit), o RELOGIO entre rounds e a
        evidencia duravel no banco dele. O guard e dono da observacao, dos
        rounds, dos limites e da validacao.

        Uma saida so, e ela e testada em dois defeitos reais:

        * o orcamento esgotado e REMAPEADO depois de `timed_out()`, para o
          journal dizer `expired`/`expired=true` em vez de herdar o ultimo
          `human_required` — que era o que o teste de integracao consolidava;
        * **nenhuma** excecao sai daqui. Falha do runtime e defeito do guard, e
          um defeito do guard nao pode abortar uma candidatura ja preenchida:
          vira parada retomavel, com a janela fechada (`HANDOFF_FINISHED`) e o
          motivo no journal. `HANDOFF_STARTED` orfao era indistinguivel de
          processo morto na execucao seguinte.
        """
        runtime: Any | None = None
        observation: Any | None = None
        acl: ChallengeAclOutcome | None = None
        opened = False
        locked = False
        failed = ""
        try:
            runtime = self.runtime_factory(page)
            runtime.start()
            # `evaluate()` devolve a DECISAO; o retrato factual esta no monitor —
            # e e o retrato que diz se havia desafio para tratar.
            runtime.evaluate(phase=ChallengePhase.PRE_SUBMIT)
            observation = runtime.monitor.observation
            if observation is None or not observation.detected:
                return ChallengeHandlingResult(ChallengeHandling.NOT_DETECTED, observation=observation)

            acl = map_runtime_result(runtime.result())
            self._record(application_id, HANDOFF_STARTED, {
                "session_id": acl.session_id,
                "provider": acl.provider,
                "challenge_type": acl.challenge_type,
                "wait_seconds": round(float(self.wait_seconds), 3),
            })
            opened = True
            locked = _lock(self.relay, page, self.lock_submit)
            while acl.waitable:
                if self.sleep is not None:
                    # A pessoa age AQUI. O guard reobserva depois.
                    self.sleep(self.poll_seconds)
                if self.relay is not None:
                    # E e AQUI que os comandos dela sao executados: sem o drain na
                    # thread da pagina, a janela existiria e nao deixaria ninguem
                    # agir — a trava de submit so e util com a janela funcionando.
                    self.relay.drain(page)
                if runtime.budget.expired():
                    # `timed_out()` troca a decisao; sem remapear, o `acl` ficaria
                    # com o retrato ANTERIOR e o journal mentiria sobre o motivo de
                    # a janela ter fechado.
                    runtime.timed_out()
                    acl = map_runtime_result(runtime.result())
                    break
                runtime.revalidate(phase=ChallengePhase.PRE_SUBMIT)
                acl = map_runtime_result(runtime.result())
        except Exception as exc:
            failed = f"{type(exc).__name__}: {exc}"[:120]
            acl = _failed_acl(acl, failed)
        finally:
            _restore(self.relay, page, locked)
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:  # pragma: no cover - fechar nunca derruba o loop
                    pass

        if acl is None:  # pragma: no cover - defensivo: o except acima sempre cria um
            acl = _failed_acl(None, failed or "no outcome")
        if failed and not acl.blocks:
            acl = _failed_acl(acl, failed)
        handling = ChallengeHandling.CONTINUE if acl.resolved else ChallengeHandling.NEEDS_HUMAN
        result = ChallengeHandlingResult(handling, observation=observation, acl=acl, failed_with=failed)
        if opened:
            self._record(application_id, HANDOFF_FINISHED, dict(result.as_journal()))
        return result

    def _record(self, application_id: str, event: str, payload: dict[str, object]) -> None:
        """Grava a evidencia da janela. Falha de persistencia nunca derruba o loop."""
        if self.database is None:
            return
        try:
            self.database.append_application_event(application_id, event, dict(payload))
        except Exception:  # pragma: no cover - auditoria nao pode parar a candidatura
            pass


def reconcile_abandoned_handoffs(database: Any, application_id: str) -> int:
    """Fecha janelas do operador que ficaram abertas por um restart.

    Uma janela `started` sem `finished` significa que o processo morreu enquanto
    a pessoa agia. O browser nao volta, entao o que resta e registrar o
    abandono — com o motivo — e deixar a Application seguir o fluxo normal (nada
    foi escrito, e a retomada e segura por desenho).

    Idempotente: o proprio evento de abandono fecha a janela, e uma segunda
    chamada nao encontra nada pendente.
    """
    try:
        events = database.list_application_events(application_id)
    except Exception:  # pragma: no cover - banco indisponivel nao derruba o loop
        return 0

    pending: dict[str, object] = {}
    abandoned = 0
    for item in events:
        event = str(getattr(item, "event", ""))
        if event == HANDOFF_STARTED:
            pending = dict(getattr(item, "payload", {}) or {})
        elif event in {HANDOFF_FINISHED, HANDOFF_ABANDONED}:
            pending = {}
    if not pending:
        return 0
    database.append_application_event(
        application_id,
        HANDOFF_ABANDONED,
        {
            "reason": "process_restarted_with_window_open",
            "session_id": str(pending.get("session_id", "")),
            "provider": str(pending.get("provider", "")),
            "challenge_type": str(pending.get("challenge_type", "")),
        },
    )
    abandoned = 1
    return abandoned




# -- helpers de janela ---------------------------------------------------------


def _lock(relay: LiveViewRelay | None, page: Any, lock_submit: bool) -> bool:
    """Tira o submit do alcance ANTES de qualquer comando ser aceito."""
    if relay is None or not lock_submit:
        return False
    relay.lock_submit(page)
    return True


def _restore(relay: LiveViewRelay | None, page: Any, locked: bool) -> None:
    if not locked or relay is None:
        return
    try:
        relay.restore_submit(page)
    except Exception:  # pragma: no cover - restaurar nunca derruba o loop
        pass


def _failed_acl(acl: ChallengeAclOutcome | None, failed: str) -> ChallengeAclOutcome:
    """Resultado fail-safe: para, retomavel, com o motivo no journal.

    `acl=None` e a falha ANTES de qualquer retrato do guard (o runtime nem
    ligou): nao ha final_status do guard a preservar, e o vocabulario do host diz
    por que.
    """
    return ChallengeAclOutcome(
        handling=ChallengeAclHandling.NEEDS_HUMAN,
        final_status=acl.final_status if acl is not None else "runtime_error",
        reason_token=acl.reason_token if acl is not None else "",
        state=ApplicationState.NEEDS_CAPTCHA,
        human_required=True,
        expired=acl.expired if acl is not None else False,
        detected=acl.detected if acl is not None else False,
        session_id=acl.session_id if acl is not None else "",
        provider=acl.provider if acl is not None else "",
        challenge_type=acl.challenge_type if acl is not None else "",
        rounds=acl.rounds if acl is not None else 0,
        confidence=acl.confidence if acl is not None else 0.0,
        capability=acl.capability if acl is not None else "",
        backend=acl.backend if acl is not None else "core",
        reason=f"challenge runtime failed: {failed}",
    )


#: Poll padrao entre rounds na janela do operador.
DEFAULT_POLL_SECONDS = 0.5


def production_challenge_integration(
    *,
    database: Any,
    relay: LiveViewRelay | None,
    wait_seconds: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> ChallengeIntegration:
    """A composicao de PRODUCAO: o runtime publico do guard (CG-036).

    Existe como funcao — e nao inline em `pipeline.loop_runtime` — para que o
    caminho de producao seja o MESMO que o teste exercita. Uma composicao que so
    o CLI monta e que nenhum teste chama e uma composicao nao verificada.
    """
    import time as _time

    return ChallengeIntegration(
        runtime_factory=lambda page: runtime_for_page(
            page, wait_seconds=wait_seconds, poll_seconds=poll_seconds
        ),
        relay=relay,
        database=database,
        wait_seconds=wait_seconds,
        sleep=sleep or _time.sleep,
        poll_seconds=poll_seconds,
    )


def _detach(observer: object) -> None:
    detach = getattr(observer, "detach", None)
    if callable(detach):
        try:
            detach()
        except Exception:  # pragma: no cover - desanexar nunca derruba o loop
            pass


def _failed_outcome(session: ChallengeSession) -> OrchestratorOutcome:
    """Outcome minimo e valido para um orquestrador que levantou."""
    from datetime import datetime, timezone

    from challenge_resolution.provenance import ChallengeProvenance
    from challenge_resolution.types import ResolutionStatus

    now = datetime.now(timezone.utc)
    return OrchestratorOutcome(
        resolved=False,
        rounds=0,
        duration_seconds=0.0,
        final_status=ResolutionStatus.FAILED,
        provenance=ChallengeProvenance(
            session_id=session.session_id,
            application_id=session.application_id,
            provider=session.provider.value,
            challenge_type=session.challenge_type,
            rounds=0,
            started_at=now,
            finished_at=now,
            final_status=ResolutionStatus.FAILED.value,
            max_confidence=0.0,
        ),
    )


__all__ = [
    "ChallengeHandling",
    "ChallengeHandlingResult",
    "DEFAULT_POLL_SECONDS",
    "production_challenge_integration",
    "ChallengeIntegration",
    "HANDOFF_ABANDONED",
    "HANDOFF_FINISHED",
    "HANDOFF_STARTED",
    "reconcile_abandoned_handoffs",
]
