"""Fase 2 — `MonitorValidator`: tradução pura, célula a célula.

O validator não decide nada: ele **reobserva** pelo guard e traduz a decisão. Os
testes aqui fixam três coisas:

1. a tabela canônica cobre TODOS os membros do guard (um membro novo quebra o
   teste, forçando decisão de política em vez de cair em fail-safe silencioso);
2. o validator passa ao monitor o contexto que ele tem (`phase`,
   `browser_write_sent`, `submission_confirmed`) e entrega ao `decide` a
   observação que recebeu;
3. exceção do monitor **propaga** — quem captura e classifica é o orquestrador.

E uma coisa que é medida, não presumida: o guard **não** devolve
`provider_rejected` sem escrita. Isso está no teste
`test_a_rejection_page_without_a_write_is_not_a_rejection`, que usa o guard real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from challenge_guard.models import ChallengeDecisionStatus, ChallengePhase, ChallengeProvider, ChallengeType

from challenge_resolution.journal import InMemoryJournal
from challenge_resolution.models import OrchestratorLimits, ResolutionResult, ValidationResult
from challenge_resolution.orchestrator import ChallengeOrchestrator
from challenge_resolution.session import ChallengeSession
from challenge_resolution.types import ChallengeObservation, ResolutionStatus, ValidationStatus
from challenge_resolution.validator import MonitorValidator, _DECISION_TO_STATUS


def _session() -> ChallengeSession:
    return ChallengeSession(
        session_id="s-1",
        application_id="app-1",
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type="checkbox",
        phase=ChallengePhase.PRE_SUBMIT,
        started_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
        initial_confidence=0.9,
    )


def _observation(*, detected: bool = True, confidence: float = 0.9) -> ChallengeObservation:
    return ChallengeObservation(
        detected=detected,
        phase=ChallengePhase.PRE_SUBMIT,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type=ChallengeType.CHECKBOX,
        session_id="s-1",
        visible=True,
        confidence=confidence,
    )


@dataclass
class _Decision:
    status: object


class ScriptedMonitor:
    """Monitor com histórico: registra o que o validator pediu e o que entregou."""

    def __init__(self, *, decision: object = None, observation: ChallengeObservation | None = None) -> None:
        self.decision = _Decision(decision) if decision is not None else _Decision(ChallengeDecisionStatus.NONE)
        self.observation = observation or _observation()
        self.observe_calls: list[dict[str, object]] = []
        self.decide_observations: list[ChallengeObservation] = []

    def observe(self, **kwargs: object) -> ChallengeObservation:
        self.observe_calls.append(dict(kwargs))
        return self.observation

    def decide(self, observation: ChallengeObservation) -> object:
        self.decide_observations.append(observation)
        return self.decision


class _ExplodingObserve(ScriptedMonitor):
    def observe(self, **kwargs: object) -> ChallengeObservation:
        raise RuntimeError("monitor observe explodiu")


class _ExplodingDecide(ScriptedMonitor):
    def decide(self, observation: ChallengeObservation) -> object:
        raise ValueError("decide explodiu")


# --- tabela canônica -------------------------------------------------------

def test_every_guard_decision_has_a_mapping() -> None:
    """Cobertura completa: um membro novo do guard tem de ser decidido aqui."""
    missing = [member.name for member in ChallengeDecisionStatus if member not in _DECISION_TO_STATUS]
    assert missing == [], f"decisões sem mapeamento: {missing}"


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (ChallengeDecisionStatus.RESOLVED_EXTERNALLY, ValidationStatus.ACCEPTED),
        (ChallengeDecisionStatus.PROVIDER_REJECTED, ValidationStatus.PROVIDER_REJECTED),
        (ChallengeDecisionStatus.NEEDS_HUMAN, ValidationStatus.REPEATED),
        (ChallengeDecisionStatus.OBSERVE, ValidationStatus.INCONCLUSIVE),
        (ChallengeDecisionStatus.NONE, ValidationStatus.ACCEPTED),
        (ChallengeDecisionStatus.UNKNOWN, ValidationStatus.INCONCLUSIVE),
    ],
    ids=lambda value: getattr(value, "name", value),
)
def test_the_canonical_mapping_cell_by_cell(decision: ChallengeDecisionStatus, expected: ValidationStatus) -> None:
    monitor = ScriptedMonitor(decision=decision)

    result = MonitorValidator(monitor=monitor).validate(_session())

    assert result.status is expected
    assert result.guard_decision_status is decision


def test_an_unknown_decision_falls_back_to_inconclusive() -> None:
    """Fail-safe: o que o validador não conhece vira "outro round"."""

    class _Weird:
        name = "WEIRD"
        value = "weird"

    monitor = ScriptedMonitor(decision=_Weird())

    result = MonitorValidator(monitor=monitor).validate(_session())

    assert result.status is ValidationStatus.INCONCLUSIVE


def test_a_decision_without_status_falls_back_to_inconclusive() -> None:
    monitor = ScriptedMonitor()
    monitor.decision = object()  # type: ignore[assignment]

    result = MonitorValidator(monitor=monitor).validate(_session())

    assert result.status is ValidationStatus.INCONCLUSIVE
    assert result.guard_decision_status is None


# --- contexto passado ao monitor ------------------------------------------

def test_the_validator_passes_the_phase_and_the_flags_to_the_monitor() -> None:
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.RESOLVED_EXTERNALLY)

    MonitorValidator(monitor=monitor).validate(_session(), browser_write_sent=True, submission_confirmed=True)

    assert monitor.observe_calls == [
        {
            "phase": ChallengePhase.PRE_SUBMIT,
            "browser_write_sent": True,
            "submission_confirmed": True,
            "response_texts": (),
        }
    ]


def test_the_validator_forwards_the_page_evidence_to_the_monitor() -> None:
    """O texto da recusa é o que permite ao guard concluir `provider_rejected`."""
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.PROVIDER_REJECTED)

    MonitorValidator(monitor=monitor).validate(
        _session(),
        browser_write_sent=True,
        page_errors=["There was an error verifying your application.", "Please try again."],
    )

    assert monitor.observe_calls[0]["response_texts"] == (
        "There was an error verifying your application.",
        "Please try again.",
    )


def test_the_flags_default_to_false() -> None:
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.NONE)

    MonitorValidator(monitor=monitor).validate(_session())

    assert monitor.observe_calls[0]["browser_write_sent"] is False
    assert monitor.observe_calls[0]["submission_confirmed"] is False


def test_decide_receives_exactly_the_observation_that_observe_returned() -> None:
    observation = _observation(confidence=0.77)
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.NEEDS_HUMAN, observation=observation)

    result = MonitorValidator(monitor=monitor).validate(_session())

    assert monitor.decide_observations == [observation]
    assert result.observation is observation


def test_the_result_is_frozen_and_carries_no_secret() -> None:
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.PROVIDER_REJECTED)

    result = MonitorValidator(monitor=monitor).validate(_session(), browser_write_sent=True)

    assert isinstance(result, ValidationResult)
    assert result.provider_rejected is True
    assert result.accepted is False and result.repeated is False


# --- exceções propagam ----------------------------------------------------

def test_an_observe_exception_propagates() -> None:
    with pytest.raises(RuntimeError, match="observe explodiu"):
        MonitorValidator(monitor=_ExplodingObserve()).validate(_session())


def test_a_decide_exception_propagates() -> None:
    with pytest.raises(ValueError, match="decide explodiu"):
        MonitorValidator(monitor=_ExplodingDecide()).validate(_session())


# --- integração com o orquestrador ---------------------------------------

@dataclass
class _Engine:
    status: ResolutionStatus = ResolutionStatus.RESOLVED
    calls: int = 0

    def resolve(self, session: ChallengeSession, observation: object, executor: object) -> ResolutionResult:
        self.calls += 1
        return ResolutionResult(
            status=self.status,
            provider=observation.provider,  # type: ignore[attr-defined]
            challenge_type="checkbox",
            session_id=session.session_id,
            strategy_name="scripted",
        )


def _run(monitor: ScriptedMonitor, *, max_rounds: int = 2) -> tuple[object, InMemoryJournal]:
    journal = InMemoryJournal()
    orchestrator = ChallengeOrchestrator(
        monitor=monitor,
        engine=_Engine(),
        validator=MonitorValidator(monitor=monitor),
        journal=journal,
        limits=OrchestratorLimits(max_rounds=max_rounds, timeout_seconds=5, max_duration_seconds=10),
    )
    return orchestrator.run(_session()), journal


def test_the_orchestrator_journal_records_the_guard_decision() -> None:
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.RESOLVED_EXTERNALLY)

    outcome, journal = _run(monitor)

    assert outcome.final_status is ResolutionStatus.RESOLVED
    event = journal.last("challenge_validation_result")
    assert event is not None
    assert event.fields["status"] == "accepted"
    assert event.fields["guard_decision_status"] == "resolved_externally"


def test_inconclusive_and_repeated_make_the_orchestrator_try_again() -> None:
    """`observe` (ambiguidade) não é aceitação: o orquestrador volta a tentar."""
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.OBSERVE)

    outcome, journal = _run(monitor, max_rounds=2)

    assert outcome.rounds == 2
    assert outcome.final_status is ResolutionStatus.EXPIRED
    assert [event.fields["limit"] for event in journal.find("challenge_limit_exceeded")] == ["max_rounds"]
    assert journal.last("challenge_validation_result").fields["guard_decision_status"] == "observe"


def test_provider_rejection_terminates_the_orchestration() -> None:
    monitor = ScriptedMonitor(decision=ChallengeDecisionStatus.PROVIDER_REJECTED)

    outcome, journal = _run(monitor)

    assert outcome.final_status is ResolutionStatus.FAILED
    assert journal.last("challenge_validation_result").fields["status"] == "provider_rejected"


# --- o guard real, para a regra que NÃO está no validator ------------------

@pytest.mark.integration
def test_a_rejection_page_without_a_write_is_not_a_rejection() -> None:
    """A garantia que retirei do validator, medida no guard real.

    Eu tinha uma regra no validator: "recusa sem escrita vira REPEATED". O plano
    descartou essa regra argumentando que o guard já consulta `browser_write_sent`
    e que uma segunda camada seria política oculta. Medido: **sem escrita o guard
    NUNCA diz `provider_rejected`** — ele diz `needs_human` (desafio visível, sem
    o texto do erro) ou `observe` (com o texto), conforme a evidência que o
    chamador entrega. Com escrita, diz `provider_rejected`.

    O invariante é "recusa exige escrita", e ele vive no guard. O validator não
    reescreve nada: traduz o que recebeu.

    E é por isso que o canal `page_errors` teve de entrar no contrato: sem o
    TEXTO da recusa, o guard não conclui `provider_rejected` nem com a escrita —
    a decisão fica em `observe`. Um envio recusado seria lido como "pendente".
    """
    pytest.importorskip("playwright.sync_api", reason="medição exige Chromium real")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from playwright.sync_api import sync_playwright

    from challenge_guard.monitor import ChallengeMonitor

    page_html = (
        "<!doctype html><html><body><h1>There was an error</h1>"
        '<div class="error" role="alert">There was an error verifying your application. '
        "Please try again.</div>"
        '<div class="g-recaptcha" data-sitekey="k" style="width:304px;height:78px">robot</div>'
        "</body></html>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - protocolo
            body = page_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/x", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            evidence = ["There was an error verifying your application. Please try again."]
            seen: dict[bool, ValidationStatus] = {}
            for write_sent in (False, True):
                monitor = ChallengeMonitor()
                monitor.attach(page)
                result = MonitorValidator(monitor=monitor).validate(
                    _session(), browser_write_sent=write_sent, page_errors=evidence
                )
                monitor.detach()
                seen[write_sent] = result.status

            # Sem escrita NÃO existe recusa de candidatura — qualquer que seja a
            # evidência disponível, o veredito nunca é PROVIDER_REJECTED.
            assert seen[False] is not ValidationStatus.PROVIDER_REJECTED, seen
            # Com escrita, a recusa é reconhecida.
            assert seen[True] is ValidationStatus.PROVIDER_REJECTED, seen
            browser.close()
    finally:
        server.shutdown()
