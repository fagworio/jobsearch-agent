"""Invariantes que valem em TODOS os cenários.

Se algum destes falhar, a flag não pode ser ligada — independentemente de
qualquer outro teste passar. Cada cenário roda o loop **real**, com o
`challenge-guard` real, contra o ATS controlado; o que se afirma é o que
sobreviveu: escritas, identificadores do material e do contrato acumulado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

pytest.importorskip("playwright.sync_api", reason="integração exige Chromium real")

pytestmark = pytest.mark.integration

from jobsearch_agent.models import ApplicationState
from tests.integration.challenge_ats import ChallengeCapableATS
from tests.integration.harness import build_harness, remove_challenge_marker


@dataclass(frozen=True)
class Scenario:
    name: str
    ats: dict[str, Any]
    resolution_enabled: bool
    captcha_wait: float
    on_wait: Callable[[Any], None] | None
    expected_state: ApplicationState
    expected_writes: int
    resends: bool = field(default=False)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="before_post_resolved",
        ats={"challenge_before": True},
        resolution_enabled=True,
        captcha_wait=3.0,
        on_wait=remove_challenge_marker,
        expected_state=ApplicationState.SUBMITTED,
        expected_writes=1,
    ),
    Scenario(
        name="before_post_unresolved",
        ats={"challenge_before": True},
        resolution_enabled=True,
        captcha_wait=0.2,
        on_wait=None,
        expected_state=ApplicationState.NEEDS_CAPTCHA,
        expected_writes=0,
    ),
    Scenario(
        name="after_post_rejected",
        ats={"reject_after_write": True},
        resolution_enabled=True,
        captcha_wait=0.2,
        on_wait=None,
        expected_state=ApplicationState.NEEDS_HUMAN_CAPTCHA,
        expected_writes=1,
    ),
    Scenario(
        name="flag_off_happy",
        ats={},
        resolution_enabled=False,
        captcha_wait=0.0,
        on_wait=None,
        expected_state=ApplicationState.SUBMITTED,
        expected_writes=1,
    ),
    Scenario(
        name="flag_off_with_challenge",
        ats={"challenge_before": True},
        resolution_enabled=False,
        captcha_wait=0.0,
        on_wait=None,
        expected_state=ApplicationState.NEEDS_CAPTCHA,
        expected_writes=0,
    ),
)


def _ids(scenario: Scenario) -> str:
    return scenario.name


def _run(tmp_path: Path, scenario: Scenario):
    with ChallengeCapableATS(**scenario.ats) as ats:
        harness = build_harness(
            tmp_path,
            ats,
            resolution_enabled=scenario.resolution_enabled,
            captcha_wait=scenario.captcha_wait,
            poll_seconds=0.05,
            on_wait=scenario.on_wait,
        )
        result = harness.run()
        return harness, ats, result


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids)
def test_invariant_the_budget_is_never_exceeded(tmp_path: Path, scenario: Scenario):
    """Nunca mais de UMA escrita de submissão — em nenhum cenário.

    Três fontes independentes: o resultado do loop, o contador do guard real e o
    que o SERVIDOR recebeu.
    """
    harness, ats, result = _run(tmp_path, scenario)

    assert result.state is scenario.expected_state
    assert result.submission_writes == scenario.expected_writes
    assert harness.writes() <= 1
    assert len(ats.posts) <= 1
    assert result.submission_writes <= 1


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids)
def test_invariant_the_material_never_changes(tmp_path: Path, scenario: Scenario):
    """`resume_sha256` e `answers_fingerprint` são imutáveis em todos os cenários.

    O desafio pode ter alterado o DOM; o material aprovado, nunca.
    """
    harness, _ats, result = _run(tmp_path, scenario)

    application = harness.application()
    context = application.context
    assert context["resume_sha256"] == harness.resume_sha256
    assert result.resume_sha256 == harness.resume_sha256
    assert result.answers_fingerprint == context["journey"]["answers_fingerprint"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids)
def test_invariant_no_attempt_ever_claims_an_undelivered_write(tmp_path: Path, scenario: Scenario):
    """O que o registro PODE afirmar sobre a entrega.

    Uma tentativa pode existir sem escrita — é o caso do desafio descoberto no
    momento do submit (`captcha_no_write`), em que nada saiu do browser. O que
    não pode existir é uma tentativa **afirmando** entrega que o guard não fez.

    `submit_write: True` e o status `SUBMITTED` são as duas formas de o registro
    dizer "isto foi entregue", e as duas contam para o mesmo teto.
    """
    harness, _ats, result = _run(tmp_path, scenario)

    application = harness.application()
    intents = harness.database.list_submission_intents(application.id)
    attempts = harness.database.list_submission_attempts(application.id)

    assert len(intents) <= 1
    assert len(attempts) <= 1

    delivered = [
        attempt
        for attempt in attempts
        if attempt.evidence.get("submit_write") is True
        or attempt.status == ApplicationState.SUBMITTED.value
    ]
    assert len(delivered) <= 1
    assert len(delivered) == scenario.expected_writes
    assert len(delivered) == harness.writes()

    if scenario.expected_writes == 0:
        # Nenhuma tentativa pode estar num status que signifique "entregue".
        for attempt in attempts:
            assert attempt.status not in {
                ApplicationState.SUBMITTED.value,
                ApplicationState.NEEDS_HUMAN_CAPTCHA.value,
            }
            assert attempt.evidence.get("submit_write") in {None, False}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_ids)
def test_invariant_the_form_contract_is_preserved(tmp_path: Path, scenario: Scenario):
    """O preenchimento não se perde, mesmo quando o desafio interrompe."""
    _harness, _ats, result = _run(tmp_path, scenario)

    assert result.unanswered_required == ()
    assert result.questions_answered >= 3
    assert result.form_fingerprint


def test_invariant_the_runtime_never_exposes_write_types():
    """O pacote de resolução não expõe tipos da fronteira de escrita.

    Reforço em runtime do contrato estático: uma reexportação acidental seria
    visível aqui mesmo que o import-linter passasse.
    """
    import challenge_resolution

    forbidden = {"AuthorizedWrite", "SubmissionIntent", "NetworkWriteGuard", "LiveNetworkPolicy"}
    assert forbidden.isdisjoint(set(dir(challenge_resolution)))


def test_invariant_the_agent_only_knows_the_package_through_the_integration():
    """Fase 6: a integração existe, e é explícita.

    O invariante deixou de ser "não importe" e passou a ser "só a integração
    importa": loop, browser e submissão não podem ganhar conhecimento do motor
    de resolução por conveniência.
    """
    import ast
    from pathlib import Path as _Path

    allowed = {"challenge_integration.py", "challenge_strategies.py"}
    root = _Path(__file__).parents[2] / "src" / "jobsearch_agent"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("challenge_resolution"):
                offenders.append(path.name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("challenge_resolution"):
                        offenders.append(path.name)
    assert offenders == [], f"fora da integração: {offenders}"
