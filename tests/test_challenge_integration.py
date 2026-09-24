"""JSA-CG-001 .. JSA-CG-014: integração com o challenge-guard.

A dependência precisa ficar REALMENTE isolada: só a ACL fala com a biblioteca, os
orçamentos de rede são independentes, e os desfechos medidos em produção
continuam iguais.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from jobsearch_agent.browser import ChallengeRuntimePermission, NetworkWriteGuard
from jobsearch_agent.challenges import (
    ChallengeOutcome,
    JobsearchChallengeAdapter,
    application_state_for,
)
from jobsearch_agent.models import ApplicationState

SOURCE = pathlib.Path(__file__).parents[1] / "src" / "jobsearch_agent"


# --- JSA-CG-001: contrato da dependencia --------------------------------------


def test_the_dependency_is_a_pinned_remote_reference():
    """`path =` faria o build depender do filesystem de quem desenvolve.

    A dependencia precisa vir do repositorio publicado e fixada por referencia
    IMUTAVEL: um branch mudaria o build sem mudar o codigo, e uma tag sozinha
    pode ser movida no remoto.
    """
    import tomllib

    root = pathlib.Path(__file__).parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dep = data["tool"]["poetry"]["dependencies"]["challenge-guard"]

    assert "path" not in dep, "challenge-guard voltou a ser dependencia local (path =)"
    assert "develop" not in dep, "dependencia local (develop) nao pode voltar"
    assert dep.get("git"), "challenge-guard precisa vir do repositorio publicado"
    revision = dep.get("rev") or ""
    assert revision, "a referencia precisa ser um SHA imutavel (rev), nao branch nem tag solta"
    assert all(ch in "0123456789abcdef" for ch in revision) and len(revision) == 40, (
        f"rev precisa ser o SHA completo de 40 hexadecimais, veio {revision!r}"
    )


def test_the_challenge_guard_is_importable_with_the_expected_api():
    import challenge_guard

    assert challenge_guard.__version__
    for name in (
        "ChallengeDecisionStatus",
        "ChallengeObservation",
        "ChallengePhase",
        "ChallengePolicy",
        "ChallengeProvider",
        "ChallengeSessionTracker",
        "requirements_for",
    ):
        assert hasattr(challenge_guard, name), f"API ausente: {name}"
    from challenge_guard.browser import PlaywrightChallengeAdapter

    assert PlaywrightChallengeAdapter is not None


# --- JSA-CG-002: a ACL e a unica ponte ----------------------------------------


def test_only_the_acl_imports_the_challenge_guard():
    """Se outro modulo importar a biblioteca, a independencia do dominio acabou."""
    offenders: list[str] = []
    for source in SOURCE.rglob("*.py"):
        if source.name == "challenges.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == "challenge_guard" for alias in node.names):
                    offenders.append(f"{source.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == "challenge_guard":
                    offenders.append(f"{source.name}:{node.lineno}")
    assert not offenders, f"import direto fora da ACL: {offenders}"


def test_the_executor_has_no_anti_bot_knowledge():
    """O executor conhece ChallengeOutcome, nao hCaptcha/reCAPTCHA/428."""
    text = (SOURCE / "submission_browser.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("recaptcha", "hcaptcha", "turnstile", "captcha_markers", "_captcha_visible", "_captcha_demanded"):
        assert forbidden not in text, f"conhecimento anti-bot no executor: {forbidden}"


def test_the_ats_profile_has_no_anti_bot_knowledge():
    text = (SOURCE / "providers.py").read_text(encoding="utf-8").casefold()
    for forbidden in ("hcaptcha", "recaptcha", "turnstile", "challenge_write_origins"):
        assert forbidden not in text, f"conhecimento anti-bot no perfil de ATS: {forbidden}"


def test_the_removed_debt_is_really_gone():
    removed = ("CAPTCHA_MARKERS", "CHALLENGE_WRITE_BUDGET", "challenge_write_origins", "_captcha_visible", "_captcha_demanded")
    for source in SOURCE.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for name in removed:
            assert f"def {name}" not in text
            assert f"{name} =" not in text, f"{name} ainda definido em {source.name}"


# --- JSA-CG-003: mapping de estados -------------------------------------------


@pytest.mark.parametrize(
    "decision,write,expected",
    [
        ("none", True, None),
        ("observe", True, None),
        ("resolved_externally", True, None),
        ("needs_human", False, ApplicationState.NEEDS_CAPTCHA.value),
        ("provider_rejected", True, ApplicationState.NEEDS_HUMAN_CAPTCHA.value),
        # `unknown` sem escrita nao pode inventar falha de candidatura.
        ("unknown", False, None),
        # `unknown` com escrita preserva exatamente-uma-vez.
        ("unknown", True, ApplicationState.SUBMIT_UNKNOWN.value),
    ],
)
def test_state_mapping(decision, write, expected):
    assert application_state_for(decision, browser_write_sent=write) == expected


# --- JSA-CG-008/009: boundary de rede e orcamentos ----------------------------


def test_a_challenge_runtime_permission_refuses_an_open_path():
    with pytest.raises(ValueError):
        ChallengeRuntimePermission(provider="hcaptcha", origin="hcaptcha.com", path_pattern=r"^/.*$")


def test_the_three_budgets_are_independent():
    guard = NetworkWriteGuard({"hcaptcha.com"})
    guard.arm_challenge_runtime(
        [ChallengeRuntimePermission(provider="hcaptcha", origin="hcaptcha.com", path_pattern=r"^/getcaptcha/", max_requests=2)]
    )
    request = type("R", (), {"method": "POST", "url": "https://hcaptcha.com/getcaptcha/1", "resource_type": "xhr"})()
    assert guard.inspect(request) is True
    assert guard.challenge_runtime_used == 1
    # Consumir challenge nao toca em escrita nem em inspecao.
    assert guard.authorized_writes_used == 0
    assert guard.inspections_used == 0


def test_the_runtime_permissions_come_from_the_guard_not_the_ats():
    permissions = JobsearchChallengeAdapter().runtime_permissions(["hcaptcha"])
    assert permissions, "hCaptcha deveria declarar requisitos"
    for permission in permissions:
        assert permission.path_pattern not in {r"^/.*$", "^.*$"}
        assert permission.origin.startswith("https://")
        assert permission.max_requests >= 1
    # Um provider sem requisito declarado nao ganha concessao nenhuma.
    assert JobsearchChallengeAdapter().runtime_permissions(["generic"]) == []
    assert JobsearchChallengeAdapter().runtime_permissions(["desconhecido"]) == []


def test_the_acl_binds_a_requirement_to_a_scoped_permission():
    permissions = JobsearchChallengeAdapter().runtime_permissions(["hcaptcha"])
    assert any(permission.covers("POST", "https://api.hcaptcha.com/getcaptcha/abc") for permission in permissions)
    assert not any(permission.covers("POST", "https://api.hcaptcha.com/qualqueroutracoisa") for permission in permissions)
    assert not any(permission.covers("POST", "https://exemplo.com/getcaptcha/abc") for permission in permissions)


def test_the_acl_only_imports_the_public_api():
    """A biblioteca precisa poder ser refatorada por dentro sem quebrar isto."""
    tree = ast.parse((SOURCE / "challenges.py").read_text(encoding="utf-8"))
    internals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("challenge_guard"):
            if node.module != "challenge_guard":
                internals.append(f"{node.module}:{node.lineno}")
    assert not internals, f"a ACL importa internals: {internals}"


def test_an_unknown_provider_does_not_widen_the_pre_detection_union():
    """`PreDetectionChallengeRuntimePolicy`: uniao FINITA das capabilities conhecidas."""
    adapter = JobsearchChallengeAdapter()
    union = adapter.pre_detection_runtime_permissions()
    extended = adapter.pre_detection_runtime_permissions() + adapter.runtime_permissions(["inexistente", ""])
    assert len(extended) == len(union)
    assert adapter.runtime_permissions(["inexistente"]) == []


def test_generic_adds_no_permit_to_the_union():
    """`GENERIC` nao declara requisito: 'nao sei' nunca vira 'pode tudo'."""
    adapter = JobsearchChallengeAdapter()
    with_generic = adapter.runtime_permissions(["hcaptcha", "generic"])
    without = adapter.runtime_permissions(["hcaptcha"])
    assert len(with_generic) == len(without)


def test_an_unused_permit_consumes_nothing():
    adapter = JobsearchChallengeAdapter()
    guard = NetworkWriteGuard({"hcaptcha.com"})
    guard.arm_challenge_runtime(adapter.pre_detection_runtime_permissions())
    assert guard.challenge_runtime_used == 0
    assert guard.authorized_writes_used == 0
    assert guard.inspections_used == 0


def test_a_presented_challenge_produces_a_handoff_the_host_can_enrich():
    """O handoff e neutro: sem vaga, sem curriculo, sem candidato."""
    outcome = _outcome(
        page=_Page(iframes=[_Iframe("https://www.recaptcha.net/recaptcha/enterprise/bframe")]),
        writes=0,
    )
    assert outcome.decision == "needs_human"
    assert outcome.handoff["continuation"] == "manual"
    assert outcome.handoff["provider"] == "recaptcha_enterprise"
    assert outcome.session_id
    blob = str(outcome.handoff).casefold()
    for forbidden in ("job", "resume", "candidate", "curriculo", "vaga", "@"):
        assert forbidden not in blob


def test_a_rejection_produces_a_final_handoff():
    outcome = _outcome(
        page_errors=["There was an error verifying your application."], http_status=400, writes=1
    )
    assert outcome.handoff["continuation"] == "manual_final"


def test_observe_and_resolution_produce_no_handoff():
    """Nada a pedir a ninguem: nao ha handoff para observar nem para resolvido."""
    watching = _outcome(
        page=_Page(iframes=[_Iframe("https://www.recaptcha.net/recaptcha/enterprise/anchor")]), writes=0
    )
    assert watching.decision == "observe"
    assert watching.handoff == {}


def test_the_acl_offers_the_read_hosts_the_widget_needs():
    hosts = JobsearchChallengeAdapter().runtime_read_hosts()
    assert any("hcaptcha" in host for host in hosts)
    assert any("recaptcha" in host for host in hosts)


# --- regressões ---------------------------------------------------------------


class _Iframe:
    def __init__(self, src: str, *, width: int = 300, height: int = 400, visible: bool = True) -> None:
        self._src, self._width, self._height, self._visible = src, width, height, visible

    def get_attribute(self, name: str) -> str:
        return self._src if name == "src" else ""

    def is_visible(self) -> bool:
        return self._visible

    def bounding_box(self) -> dict:
        return {"width": self._width, "height": self._height}


class _Page:
    """Pagina minima para a ACL: entrega DOM e frames, nada mais."""

    url = "https://example.invalid/"

    def __init__(self, html: str = "", iframes: list | None = None) -> None:
        self._html, self._iframes = html, list(iframes or [])

    def on(self, *_a) -> None:
        return None

    def remove_listener(self, *_a) -> None:
        return None

    def content(self) -> str:
        return self._html

    def query_selector_all(self, selector: str) -> list:
        return list(self._iframes) if selector == "iframe" else []


def _outcome(*, page=None, page_errors=(), http_status=None, writes=0, confirmed=False) -> ChallengeOutcome:
    adapter = JobsearchChallengeAdapter()
    if page is not None:
        adapter.attach(page)
    return adapter.observe(
        step="post_submit",
        http_status=http_status,
        page_errors=page_errors,
        browser_write_sent=bool(writes),
        submission_confirmed=confirmed,
    )


def test_regression_hcaptcha_rejection_after_a_real_write():
    """Desfecho medido em producao: POST saiu, hCaptcha recusou a verificacao."""
    outcome = _outcome(
        page_errors=["There was an error verifying your application. Please try again."],
        http_status=400,
        writes=1,
    )
    assert outcome.decision == "provider_rejected"
    assert outcome.application_state == ApplicationState.NEEDS_HUMAN_CAPTCHA.value
    # Esta e a evidencia do challenge-guard. `submit_write` e
    # `captcha_verification_failed` pertencem a evidencia de SUBMISSAO, que e
    # outro modelo: tem teste proprio em test_live_apply. Misturar os dois foi
    # erro meu ao escrever este teste.
    assert outcome.evidence["reason_token"] == "provider_rejected_submission"
    assert outcome.evidence["decision"] == "provider_rejected"
    assert outcome.evidence["http_status"] == 400
    assert set(outcome.evidence) == {
        "session_id",
        "provider",
        "phase",
        "decision",
        "reason_token",
        "sources",
        "signal_kinds",
        "http_status",
        "path_hashes",
        "rounds_observed",
        "confidence",
    }


def test_regression_enterprise_anchor_is_observed_then_rejected_at_submit():
    """Duas etapas: selo Enterprise nao bloqueia; o 428 no submit recusa.

    Antes, 'reCAPTCHA presente' viraria 'humano' cedo demais. O caminho do frame
    (`anchor` = selo, `bframe` = desafio) separa as duas coisas.
    """
    anchor = _outcome(
        page=_Page(iframes=[_Iframe("https://www.recaptcha.net/recaptcha/enterprise/anchor")]),
        writes=0,
    )
    assert anchor.decision == "observe"
    assert anchor.application_state is None

    rejected = _outcome(
        page_errors=["Please complete the reCAPTCHA and resubmit your application."],
        http_status=428,
        writes=1,
    )
    assert rejected.decision == "provider_rejected"
    assert rejected.application_state == ApplicationState.NEEDS_HUMAN_CAPTCHA.value


def test_regression_a_presented_challenge_before_submit_needs_a_human():
    presented = _outcome(
        page=_Page(iframes=[_Iframe("https://www.recaptcha.net/recaptcha/enterprise/bframe")]),
        writes=0,
    )
    assert presented.decision == "needs_human"
    assert presented.application_state == ApplicationState.NEEDS_CAPTCHA.value


def test_regression_a_confirmed_submission_beats_a_residual_widget():
    outcome = _outcome(
        page=_Page(iframes=[_Iframe("https://www.recaptcha.net/recaptcha/enterprise/bframe")]),
        http_status=200,
        writes=1,
        confirmed=True,
    )
    assert outcome.decision == "none"
    assert outcome.application_state is None
    assert outcome.reason_token == "submission_confirmed_overrides_challenge"
