"""Fase 0 do `challenge_resolution`: testes de CONTRATO, não de comportamento.

Estes testes não verificam resolução — não existe resolução ainda. Eles
verificam a arquitetura que a Fase 0 congela:

  - dataclasses imutáveis (`frozen`) e com `slots`;
  - nenhum campo com nome de material sensível;
  - o executor não recebe `AuthorizedWrite`/`SubmissionIntent`;
  - protocols são `runtime_checkable`;
  - `ResolutionPayload` recusa campo sensível na construção;
  - `OrchestratorLimits` valida os próprios invariantes;
  - `CapabilityMatrix` responde fail-safe (`UNSUPPORTED`) e reconhece os
    vocabulários reais do `challenge-guard` v0.1.0;
  - `NullStrategy` nunca inventa sucesso;
  - projeções de journal não carregam segredo.

Se um destes quebrar, a fase seguinte está construída sobre areia.
"""

from __future__ import annotations

import dataclasses
import inspect
import re

import pytest

from challenge_guard.models import ChallengePhase, ChallengeProvider, ChallengeType

from challenge_resolution import (
    CapabilityStatus,
    ChallengeProvenance,
    ChallengeSession,
    OrchestratorLimits,
    ResolutionPayload,
    ResolutionResult,
    ResolutionStatus,
    ValidationResult,
    ValidationStatus,
)
from challenge_resolution.capabilities import (
    CapabilityMatrix,
    PROVIDER_HCAPTCHA,
    PROVIDER_RECAPTCHA,
    PROVIDER_RECAPTCHA_ENTERPRISE,
    PROVIDER_TURNSTILE,
    TYPE_CHECKBOX,
    TYPE_IMAGE_SELECTION,
    TYPE_INVISIBLE,
    TYPE_MANAGED,
    TYPE_MFA,
    TYPE_RISK_ASSESSMENT,
    TYPE_VERIFICATION,
)
from challenge_resolution.protocols import (
    ChallengeInteractionExecutor,
    ChallengeResolutionEngine,
    ChallengeResolutionOrchestrator,
    ChallengeResolutionValidator,
)
from challenge_resolution.strategies import NullStrategy, StrategyRegistry
from challenge_resolution.types import ChallengeObservation, enum_value

FORBIDDEN_FIELD_PATTERNS = re.compile(
    r"(token|cookie|secret|answer|response|jwt|bearer|credentials|password|"
    r"session_key|api_key|auth)",
    re.IGNORECASE,
)

IMMUTABLE_MODELS = (
    ResolutionPayload,
    ResolutionResult,
    ValidationResult,
    OrchestratorLimits,
    ChallengeProvenance,
    ChallengeSession,
)


def _observation(
    *,
    provider: ChallengeProvider = ChallengeProvider.RECAPTCHA,
    challenge_type: ChallengeType = ChallengeType.CHECKBOX,
    detected: bool = True,
    write_sent: bool = False,
    confidence: float = 0.8,
) -> ChallengeObservation:
    return ChallengeObservation(
        detected=detected,
        phase=ChallengePhase.PRE_SUBMIT,
        provider=provider,
        challenge_type=challenge_type,
        session_id="challenge-session-0001",
        visible=True,
        browser_write_sent=write_sent,
        confidence=confidence,
    )


def _provenance(**overrides: object) -> ChallengeProvenance:
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 9, 25, tzinfo=timezone.utc)
    values: dict[str, object] = {
        "session_id": "application-1:recaptcha:checkbox:1",
        "application_id": "application-1",
        "provider": "recaptcha",
        "challenge_type": "checkbox",
        "rounds": 1,
        "started_at": started,
        "finished_at": started + timedelta(seconds=2),
        "final_status": ResolutionStatus.HUMAN_REQUIRED.value,
        "max_confidence": 0.8,
    }
    values.update(overrides)
    return ChallengeProvenance(**values)  # type: ignore[arg-type]


# --- 1. Imutabilidade -----------------------------------------------------

@pytest.mark.parametrize("model", IMMUTABLE_MODELS, ids=lambda model: model.__name__)
def test_models_are_frozen_and_slotted(model: type) -> None:
    assert dataclasses.is_dataclass(model)
    params = model.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen, f"{model.__name__} deve ser frozen"
    assert getattr(params, "slots", False), f"{model.__name__} deve usar slots"


def test_a_frozen_model_cannot_be_mutated() -> None:
    payload = ResolutionPayload(kind="handoff")
    with pytest.raises(dataclasses.FrozenInstanceError):
        payload.kind = "click"  # type: ignore[misc]


# --- 2. Sem campos sensíveis ---------------------------------------------

@pytest.mark.parametrize("model", IMMUTABLE_MODELS, ids=lambda model: model.__name__)
def test_no_model_field_looks_sensitive(model: type) -> None:
    for field in dataclasses.fields(model):
        assert not FORBIDDEN_FIELD_PATTERNS.search(field.name), (
            f"{model.__name__}.{field.name} parece material sensível"
        )


def test_a_sensitive_field_name_breaks_the_package_at_import() -> None:
    """A garantia é de import, não de revisão: um campo novo com nome proibido
    derruba o módulo antes de qualquer journal em produção."""
    from challenge_resolution.provenance import _assert_no_sensitive_fields

    @dataclasses.dataclass(frozen=True, slots=True)
    class _Fake:
        session_id: str = ""
        challenge_token: str = ""

    with pytest.raises(TypeError, match="sensível"):
        _assert_no_sensitive_fields(_Fake)


# --- 3. O executor não expõe tipos de escrita -----------------------------

@pytest.mark.parametrize(
    "protocol_method",
    [
        ChallengeInteractionExecutor.execute,
        ChallengeResolutionEngine.resolve,
        ChallengeResolutionValidator.validate,
        ChallengeResolutionOrchestrator.run,
    ],
    ids=lambda method: method.__qualname__,
)
def test_protocol_annotations_never_mention_write_types(protocol_method: object) -> None:
    signature = inspect.signature(protocol_method)  # type: ignore[arg-type]
    rendered = " ".join(str(parameter.annotation) for parameter in signature.parameters.values())
    rendered += " " + str(signature.return_annotation)
    for forbidden in ("AuthorizedWrite", "SubmissionIntent", "NetworkWriteGuard", "LiveNetworkPolicy"):
        assert forbidden not in rendered, f"{forbidden} não pode aparecer na fronteira"


# --- 4. Protocols são runtime_checkable -----------------------------------

@pytest.mark.parametrize(
    "protocol",
    [
        ChallengeInteractionExecutor,
        ChallengeResolutionEngine,
        ChallengeResolutionValidator,
        ChallengeResolutionOrchestrator,
    ],
    ids=lambda protocol: protocol.__name__,
)
def test_protocols_are_runtime_checkable(protocol: type) -> None:
    assert getattr(protocol, "_is_runtime_protocol", False)


# --- 5. Payload recusa material sensível ----------------------------------

@pytest.mark.parametrize("key", ["token", "cookie", "secret", "answer", "response"])
def test_resolution_payload_rejects_sensitive_keys(key: str) -> None:
    with pytest.raises(ValueError, match="sensíveis"):
        ResolutionPayload(kind="handoff", data={key: "..."})


def test_resolution_payload_accepts_opaque_non_sensitive_data() -> None:
    payload = ResolutionPayload(kind="handoff", data={"hint": "operator", "round": 1})
    assert payload.data["hint"] == "operator"


# --- 6. OrchestratorLimits valida invariantes -----------------------------

def test_orchestrator_limits_reject_impossible_budgets() -> None:
    with pytest.raises(ValueError, match="max_rounds"):
        OrchestratorLimits(max_rounds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        OrchestratorLimits(timeout_seconds=0)
    with pytest.raises(ValueError, match="max_duration_seconds"):
        OrchestratorLimits(timeout_seconds=100, max_duration_seconds=50)


def test_orchestrator_limits_defaults_are_conservative() -> None:
    limits = OrchestratorLimits()
    assert limits.max_rounds == 3
    assert limits.max_duration_seconds >= limits.timeout_seconds


# --- 7. CapabilityMatrix é dado e fail-safe -------------------------------

def test_capability_matrix_is_fail_safe_for_unknown_pairs() -> None:
    matrix = CapabilityMatrix()
    assert matrix.lookup("nao-existe", "nao-existe") is CapabilityStatus.UNSUPPORTED
    assert matrix.lookup(ChallengeProvider.UNKNOWN, ChallengeType.UNKNOWN) is CapabilityStatus.UNSUPPORTED


def test_capability_matrix_reads_the_real_guard_vocabulary() -> None:
    """As chaves têm de ser as do guard v0.1.0, não sinônimos inventados."""
    matrix = CapabilityMatrix()
    assert matrix.lookup(ChallengeProvider.RECAPTCHA, ChallengeType.CHECKBOX) is CapabilityStatus.SUPPORTED
    assert matrix.lookup(ChallengeProvider.HCAPTCHA, ChallengeType.IMAGE_SELECTION) is CapabilityStatus.UNSUPPORTED
    assert matrix.lookup(ChallengeProvider.TURNSTILE, ChallengeType.INVISIBLE) is CapabilityStatus.SUPPORTED
    # `image` era o nome do rascunho; o guard chama de `image_selection`.
    assert matrix.lookup(PROVIDER_RECAPTCHA, "image") is CapabilityStatus.HUMAN_REQUIRED
    assert matrix.lookup(PROVIDER_RECAPTCHA, TYPE_IMAGE_SELECTION) is CapabilityStatus.HUMAN_REQUIRED


def test_capability_matrix_declares_the_provider_that_rejected_the_real_run() -> None:
    """A Fueled observou `recaptcha_enterprise`; a matriz não pode cair no
    fail-safe por causa do nome."""
    matrix = CapabilityMatrix()
    assert matrix.is_supported(PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_CHECKBOX)
    assert matrix.lookup(PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_INVISIBLE) is CapabilityStatus.SUPPORTED
    assert matrix.lookup(PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_IMAGE_SELECTION) is CapabilityStatus.HUMAN_REQUIRED


def test_verification_channels_are_human_by_policy_not_by_accident() -> None:
    """Código de verificação é credencial: o agente não o usa."""
    matrix = CapabilityMatrix()
    assert matrix.lookup("email", TYPE_VERIFICATION) is CapabilityStatus.HUMAN_REQUIRED
    assert matrix.lookup("sms", TYPE_MFA) is CapabilityStatus.HUMAN_REQUIRED


def test_no_capability_row_is_declared_supported_without_a_strategy_note() -> None:
    """Nenhum par pode ser SUPPORTED por engano: só os que a Fase 5+ vai cobrir."""
    matrix = CapabilityMatrix()
    supported = {key for key, status in matrix.entries().items() if status is CapabilityStatus.SUPPORTED}
    assert supported == {
        (PROVIDER_RECAPTCHA, TYPE_CHECKBOX),
        (PROVIDER_RECAPTCHA, TYPE_INVISIBLE),
        (PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_CHECKBOX),
        (PROVIDER_RECAPTCHA_ENTERPRISE, TYPE_INVISIBLE),
        (PROVIDER_HCAPTCHA, TYPE_CHECKBOX),
        (PROVIDER_TURNSTILE, TYPE_MANAGED),
        (PROVIDER_TURNSTILE, TYPE_INVISIBLE),
    }


def test_capability_matrix_entries_are_immutable() -> None:
    matrix = CapabilityMatrix()
    with pytest.raises(TypeError):
        matrix.entries()[("x", "y")] = CapabilityStatus.SUPPORTED  # type: ignore[index]


def test_risk_assessment_is_human_required_for_turnstile() -> None:
    assert CapabilityMatrix().lookup(PROVIDER_TURNSTILE, TYPE_RISK_ASSESSMENT) is CapabilityStatus.HUMAN_REQUIRED


# --- 8. NullStrategy e StrategyRegistry -----------------------------------

def test_null_strategy_never_invents_success() -> None:
    """A estratégia nula devolve UNSUPPORTED — nunca um sucesso inventado.

    Contratos da Fase 1 são SÍNCRONOS (decisão registrada em `protocols.py`):
    a chamada é direta, não por `asyncio.run`.
    """
    strategy = NullStrategy()
    observation = _observation()
    session = ChallengeSession.from_observation(observation, application_id="application-1")

    result = strategy.resolve(session, observation, executor=None)  # type: ignore[arg-type]

    assert result.status is ResolutionStatus.UNSUPPORTED
    assert result.strategy_name == "null"
    assert result.session_id == session.session_id
    assert result.challenge_type == "checkbox"


class _NamedStrategy(NullStrategy):
    def __init__(self, name: str) -> None:
        self.name = name


def test_strategy_registry_is_ordered_and_selects_the_first_match() -> None:
    first, second = _NamedStrategy("first"), _NamedStrategy("second")
    registry = StrategyRegistry([first, second])

    assert registry.names() == ("first", "second")
    assert registry.select(_observation()) is first
    assert len(registry) == 2


def test_an_empty_registry_selects_nothing() -> None:
    assert StrategyRegistry([]).select(_observation()) is None


# --- 9. Sessão e proveniência --------------------------------------------

def test_session_is_derived_from_the_observation_without_runtime_objects() -> None:
    observation = _observation(provider=ChallengeProvider.HCAPTCHA, challenge_type=ChallengeType.IMAGE_SELECTION)
    session = ChallengeSession.from_observation(observation, application_id="application-7")

    assert session.application_id == "application-7"
    assert session.provider is ChallengeProvider.HCAPTCHA
    assert session.challenge_type == "image_selection"
    assert session.phase is ChallengePhase.PRE_SUBMIT
    assert session.initial_confidence == pytest.approx(0.8)
    assert "hcaptcha" in session.session_id
    # Nada de browser/page/context na sessão: ela é serializável por construção.
    assert all(not hasattr(session, name) for name in ("page", "browser", "context"))


def test_enum_value_returns_the_canonical_value_not_the_class_name() -> None:
    """`str(ChallengeType.CHECKBOX)` devolve "ChallengeType.CHECKBOX"."""
    assert enum_value(ChallengeType.CHECKBOX) == "checkbox"
    assert enum_value("checkbox") == "checkbox"


def test_provenance_validates_its_own_invariants() -> None:
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 9, 25, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="finished_at"):
        _provenance(started_at=started, finished_at=started - timedelta(seconds=1))
    with pytest.raises(ValueError, match="max_confidence"):
        _provenance(max_confidence=1.5)
    with pytest.raises(ValueError, match="rounds"):
        _provenance(rounds=-1)


def test_provenance_journal_projection_carries_no_secret() -> None:
    projection = _provenance().as_journal()
    for key in projection:
        assert not FORBIDDEN_FIELD_PATTERNS.search(key), key
    assert projection["provider"] == "recaptcha"
    assert projection["duration_seconds"] == pytest.approx(2.0)
    assert projection["strategy_names"] == []


def test_orchestrator_outcome_journal_projection_is_bounded() -> None:
    from challenge_resolution.models import OrchestratorOutcome

    outcome = OrchestratorOutcome(
        resolved=False,
        rounds=2,
        duration_seconds=3.5,
        final_status=ResolutionStatus.HUMAN_REQUIRED,
        provenance=_provenance(rounds=2),
    )
    projection = outcome.as_journal()

    assert projection["final_status"] == "human_required"
    assert set(projection) == {
        "resolved",
        "rounds",
        "duration_seconds",
        "final_status",
        "session_id",
        "provider",
        "challenge_type",
    }


def test_validation_result_exposes_the_decision_as_properties() -> None:
    accepted = ValidationResult(status=ValidationStatus.ACCEPTED, observation=_observation())
    rejected = ValidationResult(status=ValidationStatus.PROVIDER_REJECTED, observation=_observation(write_sent=True))
    repeated = ValidationResult(status=ValidationStatus.REPEATED, observation=_observation())

    assert accepted.accepted and not accepted.provider_rejected
    assert rejected.provider_rejected and not rejected.accepted
    assert repeated.repeated and not repeated.accepted


def test_resolution_result_terminality_is_explicit() -> None:
    terminal = ResolutionResult(
        status=ResolutionStatus.RESOLVED,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type="checkbox",
        session_id="s",
    )
    retryable = ResolutionResult(
        status=ResolutionStatus.TEMPORARILY_UNAVAILABLE,
        provider=ChallengeProvider.RECAPTCHA,
        challenge_type="checkbox",
        session_id="s",
    )

    assert terminal.is_terminal and not terminal.is_retryable
    assert retryable.is_retryable and not retryable.is_terminal
