"""CG-036 — o caminho do runtime no host, sem browser.

Dois defeitos viviam aqui e nenhum teste pegava:

1. o orcamento esgotava, `runtime.timed_out()` trocava a decisao, e o `acl` NAO
   era remapeado — o journal gravava `human_required`/`expired=false`, herdado do
   retrato anterior. O teste de integracao ate consolidava isso;
2. uma excecao do runtime subia para o `ApplicationLoop` e deixava
   `HANDOFF_STARTED` sem `HANDOFF_FINISHED`, o que a execucao seguinte leria como
   "processo morreu no meio".

Estes testes usam um runtime FALSO e um banco real: o que se verifica e o
contrato de saida do host, nao o guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from challenge_guard import ChallengeRuntimeResult
from jobsearch_agent.application import ApplicationService
from jobsearch_agent.challenge_acl import ChallengeAclHandling, map_runtime_result
from jobsearch_agent.challenge_integration import (
    HANDOFF_FINISHED,
    HANDOFF_STARTED,
    ChallengeHandling,
    ChallengeIntegration,
)
from jobsearch_agent.models import ApplicationState, Job
from jobsearch_agent.persistence import Database


def _application(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "runtime-path.db")
    job = Job(id="job-runtime", source="greenhouse", external_id="1", company="Acme", title="Engineer", description="x")
    database.save_job(job, "greenhouse:1", {})
    application = ApplicationService(database).create_for_job(job.id)
    return database, application.id


def _result(**overrides) -> ChallengeRuntimeResult:
    base = {
        "detected": True,
        "decision": "needs_human",
        "final_status": "human_required",
        "reason_token": "challenge_detected_interactive",
        "session_id": "sess-runtime",
        "provider": "hcaptcha",
        "challenge_type": "checkbox",
        "phase": "pre_submit",
        "rounds": 1,
        "confidence": 0.8,
        "human_required": True,
        "capability": "human_required",
        "backend": "playwright-cdp",
        "started_at": "2026-09-25T10:00:00+00:00",
        "finished_at": "2026-09-25T10:00:01+00:00",
        "budget": {"max_rounds": 2, "rounds": 1},
    }
    base.update(overrides)
    return ChallengeRuntimeResult(**base)


def _expired_result() -> ChallengeRuntimeResult:
    return _result(
        decision="unknown",
        final_status="expired",
        reason_token="human_observation_timeout",
        capability="wait_external",
        rounds=2,
        budget={"max_rounds": 2, "rounds": 2},
    )


@dataclass
class FakeBudget:
    expirations: list[bool] = field(default_factory=list)

    def expired(self) -> bool:
        return self.expirations.pop(0) if self.expirations else False


@dataclass
class FakeRelay:
    locked: int = 0
    restored: int = 0
    drained: int = 0

    def lock_submit(self, page: object) -> int:
        self.locked += 1
        return 1

    def restore_submit(self, page: object) -> int:
        self.restored += 1
        return 1

    def drain(self, page: object) -> list:
        self.drained += 1
        return []


class FakeRuntime:
    """Runtime com roteiro: cada `result()` devolve o proximo retrato."""

    def __init__(
        self,
        results: list[ChallengeRuntimeResult],
        *,
        budget: FakeBudget | None = None,
        raise_on_revalidate: Exception | None = None,
        closed: list | None = None,
    ) -> None:
        self._results = results
        self._index = 0
        self.budget = budget or FakeBudget()
        self.monitor = SimpleNamespace(observation=SimpleNamespace(detected=True))
        self.raise_on_revalidate = raise_on_revalidate
        self.closed = closed if closed is not None else []
        self.revalidations = 0
        self.timed_out_calls = 0

    def start(self) -> "FakeRuntime":
        return self

    def evaluate(self, **kwargs: object) -> object:
        return SimpleNamespace(status="needs_human")

    def revalidate(self, **kwargs: object) -> object:
        self.revalidations += 1
        if self.raise_on_revalidate is not None:
            raise self.raise_on_revalidate
        return SimpleNamespace(resolved=False)

    def timed_out(self) -> object:
        self.timed_out_calls += 1
        return SimpleNamespace(status="unknown")

    def result(self) -> ChallengeRuntimeResult:
        value = self._results[min(self._index, len(self._results) - 1)]
        self._index += 1
        return value

    def close(self, **kwargs: object) -> None:
        self.closed.append(True)


def _integration(tmp_path: Path, runtime: FakeRuntime, relay: FakeRelay | None = None) -> tuple[ChallengeIntegration, Database, str]:
    database, application_id = _application(tmp_path)
    integration = ChallengeIntegration(
        runtime_factory=lambda page: runtime,
        relay=relay,  # type: ignore[arg-type]
        database=database,
        wait_seconds=0.4,
        sleep=lambda seconds: None,
        poll_seconds=0.05,
    )
    return integration, database, application_id


def _events(database: Database, application_id: str) -> list[tuple[str, dict]]:
    return [(event.event, dict(event.payload)) for event in database.list_application_events(application_id)]


def test_an_expired_budget_is_remapped_so_the_journal_says_expired(tmp_path: Path):
    """O bug: `timed_out()` trocava a decisao e o `acl` ficava com o retrato anterior."""
    runtime = FakeRuntime(
        [_result(), _result(), _expired_result()],
        budget=FakeBudget([False, True]),
    )
    integration, database, application_id = _integration(tmp_path, runtime)

    result = integration.handle(object(), application_id)

    assert result.acl is not None
    assert result.acl.expired is True
    assert result.acl.final_status == "expired"
    assert result.acl.state is ApplicationState.NEEDS_CAPTCHA
    assert runtime.timed_out_calls == 1
    finished = dict(_events(database, application_id))[HANDOFF_FINISHED]
    assert finished["final_status"] == "expired"
    assert finished["expired"] is True
    assert finished["handling"] == ChallengeAclHandling.NEEDS_HUMAN.value
    assert result.handling is ChallengeHandling.NEEDS_HUMAN


def test_a_runtime_exception_is_fail_safe_and_closes_the_window(tmp_path: Path):
    """Excecao nao escapa para o loop, e `HANDOFF_STARTED` nunca fica orfao."""
    relay = FakeRelay()
    runtime = FakeRuntime([_result()], raise_on_revalidate=RuntimeError("boom no runtime"))
    integration, database, application_id = _integration(tmp_path, runtime, relay)

    result = integration.handle(object(), application_id)

    assert result.handling is ChallengeHandling.NEEDS_HUMAN
    assert result.blocks is True
    assert result.acl is not None and result.acl.state is ApplicationState.NEEDS_CAPTCHA
    assert "boom no runtime" in result.failed_with
    events = dict(_events(database, application_id))
    assert HANDOFF_STARTED in events and HANDOFF_FINISHED in events
    assert "boom no runtime" in events[HANDOFF_FINISHED]["failed_with"]
    assert runtime.closed == [True], "o runtime precisa ser fechado mesmo falhando"
    assert relay.locked == 1 and relay.restored == 1, "a janela precisa ser restaurada"


def test_a_runtime_exception_before_any_decision_still_returns_fail_safe(tmp_path: Path):
    """Falha no proprio `result()` tambem para — e nao abre janela orfa."""
    runtime = FakeRuntime([_result()])
    runtime.result = lambda: (_ for _ in ()).throw(ValueError("retrato indisponivel"))  # type: ignore[method-assign]
    integration, database, application_id = _integration(tmp_path, runtime)

    result = integration.handle(object(), application_id)

    assert result.handling is ChallengeHandling.NEEDS_HUMAN
    assert result.acl is not None and result.acl.final_status == "runtime_error"
    assert result.acl.state is ApplicationState.NEEDS_CAPTCHA
    assert "retrato indisponivel" in result.failed_with
    # A janela nem chegou a abrir (a falha foi antes da deteccao): nao ha
    # `HANDOFF_STARTED`, e portanto nao ha `HANDOFF_FINISHED` a exigir.
    kinds = [kind for kind, _payload in _events(database, application_id)]
    assert HANDOFF_STARTED not in kinds and HANDOFF_FINISHED not in kinds
    assert runtime.closed == [True]


def test_the_resolved_path_continues_and_records_the_window(tmp_path: Path):
    relay = FakeRelay()
    runtime = FakeRuntime(
        [
            _result(detected=False, decision="resolved_externally", final_status="resolved_externally", reason_token="challenge_resolved_externally", human_required=False),
        ],
    )
    integration, database, application_id = _integration(tmp_path, runtime, relay)

    result = integration.handle(object(), application_id)

    assert result.handling is ChallengeHandling.CONTINUE
    assert result.acl is not None and result.acl.state is None
    events = dict(_events(database, application_id))
    assert events[HANDOFF_FINISHED]["resolved"] is True


def test_the_runtime_path_uses_the_same_acl_as_the_unit_table(tmp_path: Path):
    """A ponte entre o runtime e o dominio e a ACL, nao uma copia da tabela."""
    runtime = FakeRuntime([_result()], budget=FakeBudget([True]))
    integration, _database, application_id = _integration(tmp_path, runtime)

    result = integration.handle(object(), application_id)

    assert result.acl == map_runtime_result(_result())
    assert result.failed_with == ""
