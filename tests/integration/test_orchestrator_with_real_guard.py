"""Fase 1 + Fase 2 contra o `challenge-guard` REAL, num Chromium real.

O orquestrador foi testado com monitor/engine/validator roteirizados. Aqui a
percepção e a validação são de verdade — `ChallengeMonitor` do guard observando
uma página real, `MonitorValidator` traduzindo a decisão dele. O que permanece
roteirizado é só a INTERAÇÃO (o engine), porque a estratégia concreta é a Fase 5.

A ordem importa e é o que se prova: o orquestrador observa ANTES de tentar
resolver, e valida DEPOIS — resolvido o desafio, o guard passa a reportar
`resolved_externally` e a validação aceita.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

from challenge_guard.models import ChallengePhase, ChallengeProvider
from challenge_guard.monitor import ChallengeMonitor

from challenge_resolution.journal import InMemoryJournal
from challenge_resolution.models import OrchestratorLimits, ResolutionResult
from challenge_resolution.orchestrator import ChallengeOrchestrator
from challenge_resolution.session import ChallengeSession
from challenge_resolution.types import ChallengeObservation, ResolutionStatus
from challenge_resolution.validator import MonitorValidator
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import build_harness, remove_challenge_marker

pytestmark = pytest.mark.integration


class _PageMonitor:
    """Monitor do guard preso a uma página: `observe(phase=...)` + `decide`."""

    def __init__(self, page) -> None:
        self._monitor = ChallengeMonitor()
        self._monitor.attach(page)

    def observe(
        self,
        *,
        phase: ChallengePhase = ChallengePhase.PRE_SUBMIT,
        browser_write_sent: bool = False,
        submission_confirmed: bool = False,
        response_texts: object = (),
    ) -> ChallengeObservation:
        # TODOS os extras importam, e o Protocol declara os tres: o validador os
        # usa para separar "recusa de candidatura entregue" de "desafio ainda
        # pendente". Um wrapper que engole um deles transforma validacao em
        # excecao — foi assim que o conflito apareceu, duas vezes.
        return self._monitor.observe(
            phase=phase,
            browser_write_sent=browser_write_sent,
            submission_confirmed=submission_confirmed,
            response_texts=response_texts,
        )

    def decide(self, observation: ChallengeObservation):
        return self._monitor.decide(observation)

    def detach(self) -> None:
        self._monitor.detach()


class _Engine:
    """Interação roteirizada: opcionalmente resolve (remove o marcador).

    Remover o marcador é exatamente o efeito de um clique humano no widget — o
    que o relay do operador executa em produção.
    """

    def __init__(self, *, resolve: bool, status: ResolutionStatus = ResolutionStatus.RESOLVED, page=None) -> None:
        self._resolve = resolve
        self._status = status
        self._page = page
        self.calls = 0

    def resolve(self, session: ChallengeSession, observation: object, executor: object) -> ResolutionResult:
        self.calls += 1
        if self._resolve and self._page is not None:
            remove_challenge_marker(self._page)
        return ResolutionResult(
            status=self._status,
            provider=observation.provider,  # type: ignore[attr-defined]
            challenge_type="checkbox",
            session_id=session.session_id,
            strategy_name="scripted_interaction",
            duration_seconds=0.0,
        )


def _session_for(page) -> ChallengeSession:
    probe = _PageMonitor(page)
    observation = probe.observe(phase=ChallengePhase.PRE_SUBMIT)
    probe.detach()
    return ChallengeSession(
        session_id="s-real-guard",
        application_id="app-real-guard",
        provider=observation.provider,
        challenge_type=str(observation.challenge_type.value),
        phase=ChallengePhase.PRE_SUBMIT,
        started_at=datetime.now(timezone.utc),
        initial_confidence=float(observation.confidence),
    )


def test_a_real_challenge_is_observed_then_resolved_then_accepted(tmp_path: Path):
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)
        session = harness.runtime_session()
        page = session.page
        monitor = _PageMonitor(page)
        engine = _Engine(resolve=True, page=page)
        journal = InMemoryJournal()
        orchestrator = ChallengeOrchestrator(
            monitor=monitor,
            engine=engine,
            validator=MonitorValidator(monitor=monitor),
            journal=journal,
            limits=OrchestratorLimits(max_rounds=2, timeout_seconds=5, max_duration_seconds=10),
        )

        try:
            outcome = orchestrator.run(_session_for(page))
        finally:
            monitor.detach()
            session.close()

        assert outcome.final_status is ResolutionStatus.RESOLVED, outcome.provenance.as_journal()
        assert outcome.rounds == 1
        assert engine.calls == 1
        # A percepção foi a REAL: provider e confiança vieram do guard.
        assert outcome.provenance.provider == ChallengeProvider.RECAPTCHA.value
        assert outcome.provenance.max_confidence > 0.0
        # O ciclo completo foi registrado, na ordem.
        assert journal.kinds() == [
            "challenge_orchestration_started",
            "challenge_round_started",
            "challenge_resolution_result",
            "challenge_validation_result",
            "challenge_round_finished",
            "challenge_orchestration_finished",
        ]
        assert journal.last("challenge_validation_result").fields["status"] == "accepted"


def test_a_challenge_that_nobody_resolves_exhausts_the_rounds(tmp_path: Path):
    """Limite duro contra percepção real: o desafio persiste, o orçamento acaba."""
    with ChallengeCapableATS(challenge_before=True) as ats:
        harness = build_harness(tmp_path, ats, resolution_enabled=True, captcha_wait=0.2, poll_seconds=0.05)
        session = harness.runtime_session()
        page = session.page
        monitor = _PageMonitor(page)
        engine = _Engine(resolve=False, status=ResolutionStatus.TEMPORARILY_UNAVAILABLE)
        journal = InMemoryJournal()
        orchestrator = ChallengeOrchestrator(
            monitor=monitor,
            engine=engine,
            validator=MonitorValidator(monitor=monitor),
            journal=journal,
            limits=OrchestratorLimits(max_rounds=2, timeout_seconds=5, max_duration_seconds=10),
        )

        try:
            outcome = orchestrator.run(_session_for(page))
        finally:
            monitor.detach()
            session.close()

        assert outcome.final_status is ResolutionStatus.EXPIRED
        assert outcome.rounds == 2
        assert engine.calls == 2
        assert [event.fields["limit"] for event in journal.find("challenge_limit_exceeded")] == ["max_rounds"]
