"""JSA-AUTONOMY-CORE-001: o loop nao confunde URL do formulario com destino do POST.

O `ApplicationLoop` passava `destination=form_url` para o `SubmissionCoordinator`.
Isso so funciona quando o endereco do formulario E o endereco que recebe a
candidatura — verdade no Lever e no Greenhouse moderno, falso no Workable:

    formulario   https://apply.workable.com/<account>/j/<shortcode>/apply
    submit       https://apply.workable.com/api/v1/accounts/<account>/jobs/<shortcode>/applications

O `providers.py` ja declarava os dois; faltava o loop perguntar. Estes testes
cobrem a FIACAO de producao (o valor que o runtime de producao devolve) e o
comportamento do loop quando o hook nao existe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch_agent.ats import GreenhouseAdapter, LeverAdapter, WorkableAdapter
from jobsearch_agent.config import Settings
from jobsearch_agent.loop import ApplicationLoop, LoopRuntime
from jobsearch_agent.models import ApplicationForm, Job
from jobsearch_agent.persistence import Database
from jobsearch_agent.pipeline import loop_runtime

ROOT = Path(__file__).parents[1]
PROFILE_FIXTURES = ROOT / "tests/fixtures/e2e/profile"

WORKABLE_JOB_URL = "https://apply.workable.com/pavago/j/711D24E5DB/"
WORKABLE_FORM_URL = WORKABLE_JOB_URL + "apply"
WORKABLE_SUBMIT_URL = "https://apply.workable.com/api/v1/accounts/pavago/jobs/711D24E5DB/applications"


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_args(
        root=tmp_path / "root",
        db=str(tmp_path / "autonomy.db"),
        artifacts=str(tmp_path / "applications"),
        profile=str(PROFILE_FIXTURES / "career_profile.yaml"),
        facts=str(PROFILE_FIXTURES / "locked_facts.yaml"),
        preferences=str(PROFILE_FIXTURES / "preferences.yaml"),
        answers=str(PROFILE_FIXTURES / "answers.yaml"),
        application_policy=str(PROFILE_FIXTURES / "application_policy.yaml"),
    )


def _job(url: str, external_id: str) -> Job:
    return Job(
        id="job-autonomy",
        source="workable",
        external_id=external_id,
        company="Pavago",
        title="WordPress Developer",
        description="Build WordPress sites.",
        url=url,
    )


def test_production_runtime_separates_the_form_url_from_the_submit_endpoint(tmp_path: Path):
    """Workable: o destino do POST nao e a pagina do formulario."""
    runtime = loop_runtime(_settings(tmp_path), headless=True)
    job = _job(WORKABLE_JOB_URL, "bf59eafbd39923eb")
    adapter = WorkableAdapter()

    form_url = runtime.form_url(job, adapter)
    destination = runtime.submission_destination(job, adapter, ApplicationForm(form_id="application", provider="workable"))

    assert form_url == WORKABLE_FORM_URL
    assert destination == WORKABLE_SUBMIT_URL
    assert form_url != destination


def test_production_runtime_submits_on_the_origin_the_page_lives_on(tmp_path: Path):
    """Greenhouse: o SPA vive em `job-boards`, e e de la que o POST sai."""
    runtime = loop_runtime(_settings(tmp_path), headless=True)
    job = _job("https://job-boards.greenhouse.io/fueledcareers/jobs/5428960008", "5428960008")
    destination = runtime.submission_destination(job, GreenhouseAdapter(), ApplicationForm(form_id="application"))

    assert destination == "https://job-boards.greenhouse.io/fueledcareers/jobs/5428960008"


def test_production_runtime_keeps_the_lever_destination_in_the_apply_path(tmp_path: Path):
    """Lever: o formulario e o destino coincidem — e isso nao pode regredir."""
    runtime = loop_runtime(_settings(tmp_path), headless=True)
    job = _job("https://jobs.lever.co/spotify/2193db3f", "2193db3f")
    adapter = LeverAdapter()

    assert runtime.form_url(job, adapter) == runtime.submission_destination(
        job, adapter, ApplicationForm(form_id="application", provider="lever")
    )


def test_the_loop_falls_back_to_the_form_url_when_no_destination_is_declared(tmp_path: Path):
    """Sem o hook, o comportamento antigo continua valendo (Lever/Greenhouse)."""
    database = Database(tmp_path / "loop.db")
    job = _job(WORKABLE_JOB_URL, "bf59eafbd39923eb")
    database.save_job(job, "workable:711D24E5DB", {})
    form = ApplicationForm(form_id="application", provider="workable")

    base = loop_runtime(_settings(tmp_path), headless=True)
    plain = LoopRuntime(
        adapter_for=lambda _job: WorkableAdapter(),
        form_url=lambda _job, _adapter: WORKABLE_FORM_URL,
        profile=base.profile,
        preferences=base.preferences,
        answers=base.answers,
        prepare=lambda _job: None,  # type: ignore[arg-type,return-value]
        open_session=lambda _job, _adapter: None,
    )
    loop = ApplicationLoop(database, plain)
    assert loop._submission_destination(job, WorkableAdapter(), form, WORKABLE_FORM_URL) == WORKABLE_FORM_URL

    declared = LoopRuntime(
        adapter_for=plain.adapter_for,
        form_url=plain.form_url,
        submission_destination=lambda _job, _adapter, _form: WORKABLE_SUBMIT_URL,
        profile=plain.profile,
        preferences=plain.preferences,
        answers=plain.answers,
        prepare=plain.prepare,
        open_session=plain.open_session,
    )
    loop = ApplicationLoop(database, declared)
    assert loop._submission_destination(job, WorkableAdapter(), form, WORKABLE_FORM_URL) == WORKABLE_SUBMIT_URL
    database.close()


# --- CG-036: o caminho de PRODUCAO e o runtime publico do guard --------------


def test_the_production_runtime_uses_the_guard_runtime_path(tmp_path: Path):
    """O CLI nao pode continuar montando o gate antigo (CG-036).

    Antes disto, `loop_runtime()` configurava `challenge_gate` e nenhuma
    integracao: o caminho novo existia e passava no harness, mas o comando real
    seguia a politica inline. "Tem teste" e "e o caminho de producao" sao
    afirmacoes diferentes.
    """
    from dataclasses import replace

    from challenge_guard import ChallengeRuntime
    from jobsearch_agent.persistence import Database

    settings = replace(_settings(tmp_path), challenge_resolution_enabled=True)
    database = Database(tmp_path / "production.db")
    try:
        runtime = loop_runtime(settings, headless=True, captcha_wait=1.5, database=database)

        assert runtime.challenge_gate is None, "o gate antigo nao pode continuar ligado em paralelo"
        integration = runtime.challenge_integration
        assert integration is not None
        assert integration.runtime_factory is not None
        assert integration.database is database, "a janela precisa do MESMO banco da Application"
        assert integration.wait_seconds == 1.5

        guard_runtime = integration.runtime_factory(object())
        assert isinstance(guard_runtime, ChallengeRuntime)
        assert guard_runtime.budget.limits.max_rounds >= 1
        guard_runtime.close()
    finally:
        database.close()


def test_the_production_runtime_has_no_challenge_handling_by_default(tmp_path: Path):
    """Sem a flag, nada muda: nem gate, nem integracao, nem janela."""
    runtime = loop_runtime(_settings(tmp_path), headless=True)
    assert runtime.challenge_gate is None
    assert runtime.challenge_integration is None


def test_challenge_resolution_without_a_database_is_refused(tmp_path: Path):
    """Fail-closed: sem banco nao ha janela duravel, e sem janela duravel um
    processo que morre no meio nao deixa rastro."""
    from dataclasses import replace

    settings = replace(_settings(tmp_path), challenge_resolution_enabled=True)
    with pytest.raises(ValueError, match="requires a database"):
        loop_runtime(settings, headless=True)
