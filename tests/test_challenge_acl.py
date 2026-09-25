"""CG-036 — a ACL do host sobre o `ChallengeRuntimeResult`.

A tabela de traducao e onde o host decide o que fazer com o que o guard viu. Ela
e testada campo a campo porque cada linha corresponde a um desfecho material
diferente para a candidatura — e porque "unknown" nao pode virar estado forte por
descuido.
"""

from __future__ import annotations

import pytest

from challenge_guard import ChallengeRuntimeResult
from jobsearch_agent.challenge_acl import (
    ChallengeAclHandling,
    RuntimeBudget,
    map_runtime_result,
)
from jobsearch_agent.models import ApplicationState


def _result(**overrides) -> ChallengeRuntimeResult:
    base = {
        "detected": True,
        "decision": "needs_human",
        "final_status": "human_required",
        "reason_token": "challenge_detected_interactive",
        "session_id": "sess-1",
        "provider": "hcaptcha",
        "challenge_type": "checkbox",
        "phase": "pre_submit",
        "rounds": 2,
        "confidence": 0.8,
        "human_required": True,
        "capability": "human_required",
        "backend": "playwright-cdp",
        "started_at": "2026-09-25T10:00:00+00:00",
        "finished_at": "2026-09-25T10:00:05+00:00",
        "budget": {"max_rounds": 3, "rounds": 2},
    }
    base.update(overrides)
    return ChallengeRuntimeResult(**base)


def test_a_human_required_challenge_is_resumable_not_a_rejection():
    outcome = map_runtime_result(_result())
    assert outcome.handling is ChallengeAclHandling.NEEDS_HUMAN
    assert outcome.state is ApplicationState.NEEDS_CAPTCHA
    assert outcome.human_required is True
    assert outcome.blocks is True


def test_a_disappeared_challenge_lets_the_loop_continue():
    outcome = map_runtime_result(
        _result(
            detected=False,
            decision="resolved_externally",
            final_status="resolved_externally",
            reason_token="challenge_resolved_externally",
            human_required=False,
        )
    )
    assert outcome.handling is ChallengeAclHandling.CONTINUE
    assert outcome.state is None
    assert outcome.resolved is True


def test_no_challenge_at_all_is_not_a_challenge_event():
    outcome = map_runtime_result(
        _result(detected=False, decision="none", final_status="none", reason_token="challenge_absent", human_required=False)
    )
    assert outcome.handling is ChallengeAclHandling.NOT_DETECTED
    assert outcome.state is None


def test_a_provider_rejection_after_a_write_is_the_handoff_state():
    outcome = map_runtime_result(
        _result(
            decision="provider_rejected",
            final_status="provider_rejected",
            reason_token="provider_rejected_submission",
            capability="human_required",
        ),
        browser_write_sent=True,
    )
    assert outcome.handling is ChallengeAclHandling.PROVIDER_REJECTED
    assert outcome.state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    assert outcome.blocks is True
    assert outcome.waitable is False, "rejeicao do provedor nao melhora com mais rodadas"


def test_a_provider_rejection_without_a_write_stays_resumable():
    """Ramo defensivo: o guard nao emite isto hoje (medido), mas o host nao mente."""
    outcome = map_runtime_result(
        _result(decision="provider_rejected", final_status="provider_rejected"),
        browser_write_sent=False,
    )
    assert outcome.handling is ChallengeAclHandling.NEEDS_HUMAN
    assert outcome.state is ApplicationState.NEEDS_CAPTCHA


def test_an_expired_budget_is_resumable_and_never_a_rejection():
    outcome = map_runtime_result(
        _result(
            decision="unknown",
            final_status="expired",
            reason_token="human_observation_timeout",
            human_required=True,
            capability="wait_external",
        )
    )
    assert outcome.handling is ChallengeAclHandling.NEEDS_HUMAN
    assert outcome.state is ApplicationState.NEEDS_CAPTCHA
    assert outcome.expired is True


def test_unknown_does_not_invent_a_state():
    outcome = map_runtime_result(
        _result(detected=False, decision="unknown", final_status="unknown", reason_token="", human_required=False)
    )
    assert outcome.handling is ChallengeAclHandling.UNKNOWN
    assert outcome.state is None
    # Bloqueia a escrita (nao se escreve sem saber) e NAO espera: mais rodadas
    # nao transformam incerteza em resposta.
    assert outcome.blocks is True
    assert outcome.waitable is False


def test_a_provider_supported_challenge_becomes_an_autonomous_wait():
    """JSA-CG-039: espera autonoma nao pode ser rotulada de intervencao humana."""
    outcome = map_runtime_result(
        _result(
            decision="observe",
            final_status="observe",
            reason_token="challenge_observed_non_interactive",
            human_required=False,
            capability="provider_supported",
        )
    )
    assert outcome.handling is ChallengeAclHandling.WAIT_PROVIDER
    assert outcome.blocks is True, "nao se escreve com o challenge na tela"
    assert outcome.waitable is True, "e vale continuar observando"
    assert outcome.human_required is False
    assert outcome.state is None, "espera autonoma nao inventa estado de humano"


def test_an_observed_challenge_that_does_need_a_human_still_asks_for_one():
    outcome = map_runtime_result(
        _result(
            decision="observe",
            final_status="observe",
            reason_token="challenge_detected_interactive",
            human_required=True,
            capability="human_required",
        )
    )
    assert outcome.handling is ChallengeAclHandling.NEEDS_HUMAN
    assert outcome.state is ApplicationState.NEEDS_CAPTCHA


def test_a_provider_wait_that_expires_does_not_become_a_human_request():
    outcome = map_runtime_result(
        _result(
            decision="unknown",
            final_status="expired",
            reason_token="provider_observation_timeout",
            human_required=False,
            capability="provider_supported",
        )
    )
    assert outcome.handling is ChallengeAclHandling.WAIT_PROVIDER
    assert outcome.expired is True
    assert outcome.waitable is False, "orcamento estourado nao espera mais"
    assert outcome.blocks is True
    assert outcome.human_required is False
    assert outcome.state is ApplicationState.NEEDS_CAPTCHA, "para, mas de forma retomavel"


def test_the_journal_projection_is_closed_and_has_the_loop_vocabulary():
    payload = map_runtime_result(_result()).as_journal()
    assert set(payload) == {
        "handling",
        "resolved",
        "rounds",
        "final_status",
        "reason_token",
        "provider",
        "challenge_type",
        "session_id",
        "human_required",
        "expired",
        "capability",
        "backend",
        "reason",
    }
    assert payload["final_status"] == "human_required"
    assert payload["rounds"] == 2


def test_provenance_carries_no_url_no_secret_and_no_page_material():
    provenance = map_runtime_result(_result()).provenance()
    assert set(provenance) == {"provider", "reason_token", "session_id", "decision", "rounds", "confidence", "capability"}
    rendered = str(provenance)
    assert "http" not in rendered and "token=" not in rendered


def test_provenance_is_built_from_the_guard_fields_not_from_the_page():
    outcome = map_runtime_result(_result(provider="hcaptcha", session_id="sess-9", rounds=3))
    provenance = outcome.provenance()
    assert provenance["provider"] == "hcaptcha"
    assert provenance["session_id"] == "sess-9"
    assert provenance["rounds"] == 3


@pytest.mark.parametrize(
    "wait,poll,expected_rounds",
    [
        (0.0, 0.05, 1),      # sem espera: um round e para
        (0.4, 0.05, 8),      # 400ms em passos de 50ms
        (1.0, 1.0, 1),
        (0.0, 0.0, 1),
    ],
)
def test_the_host_clock_becomes_a_guard_budget(wait: float, poll: float, expected_rounds: int):
    limits = RuntimeBudget(wait_seconds=wait, poll_seconds=poll).limits()
    assert limits.max_rounds == expected_rounds
    assert limits.timeout_seconds >= 0.1
    assert limits.max_duration_seconds >= limits.timeout_seconds
