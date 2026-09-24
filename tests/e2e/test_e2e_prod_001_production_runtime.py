"""JSA-E2E-PROD-001: runtime de PRODUCAO ate `SUBMITTED`.

Os E2E anteriores provaram o motor de candidatura com material injetado. Este
prova a **integracao do motor com o produto que gera o material**: o curriculo
nasce do `prepare()` de verdade, passa pela validacao factual, vira TXT/DOCX/PDF,
e sao esses bytes que o browser envia.

O que continua injetado e apenas o ambiente controlado:

    origem do ATS de teste · sessao que permite loopback · policy restrita ao endpoint

O que NAO e injetado: perfil, fatos, preferencias, respostas, material,
`PreparedMaterial`, `resume.pdf`, SHA, contexto, snapshot ou intent. Tudo isso
vem do caminho de producao — `Settings`, `load_profile`, `prepare`,
`loop_runtime`, `QuestionResolver`, `SubmissionCoordinator`.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="E2E de producao exige Chromium real")

from jobsearch_agent.ats import GreenhouseAdapter
from jobsearch_agent.config import Settings
from jobsearch_agent.loop import ApplicationLoop
from jobsearch_agent.models import ApplicationState
from jobsearch_agent.persistence import Database
from jobsearch_agent.pipeline import ingest, loop_runtime
from jobsearch_agent.resolver import GroundedTemplateProvider
from jobsearch_agent.submission import LiveNetworkPolicy
from tests.e2e.multistep_ats import JOB_ID, MultiStepATS
from tests.e2e.session import LoopbackSession

ROOT = Path(__file__).parents[2]
PROFILE_FIXTURES = ROOT / "tests/fixtures/e2e/profile"
JOB_FIXTURE = ROOT / "tests/fixtures/jobs/greenhouse.json"
APPLY_PATH_PATTERN = rb"^/jobs/e2e-002/apply$".decode()
SUBMITTED = ApplicationState.SUBMITTED.value

pytestmark = pytest.mark.requires_libreoffice

LIBREOFFICE_AVAILABLE = bool(shutil.which("libreoffice") or shutil.which("soffice"))


def _settings(tmp_path: Path) -> Settings:
    """Settings de PRODUCAO apontando para fixtures sinteticas.

    `root` e um diretorio temporario de proposito: assim
    `profile/answers.local.yaml` (do candidato real) NAO entra na base de
    respostas, e o teste nao depende de arquivo ignorado pelo Git.
    """
    return Settings.from_args(
        root=tmp_path / "root",
        db=str(tmp_path / "prod.db"),
        artifacts=str(tmp_path / "applications"),
        profile=str(PROFILE_FIXTURES / "career_profile.yaml"),
        facts=str(PROFILE_FIXTURES / "locked_facts.yaml"),
        preferences=str(PROFILE_FIXTURES / "preferences.yaml"),
        answers=str(PROFILE_FIXTURES / "answers.yaml"),
        application_policy=str(PROFILE_FIXTURES / "application_policy.yaml"),
    )


def _runtime_for_controlled_ats(settings: Settings, ats: MultiStepATS, sessions: list, guards: list):
    """Runtime de PRODUCAO com apenas sessao e politica substituidas."""

    def session_factory(job, adapter):
        session = LoopbackSession(ats.origin)
        session.start()
        sessions.append(session)
        guards.append(session.network_guard)
        return session

    def policy_factory(provider, application_id, intent_id):
        return LiveNetworkPolicy(
            provider=provider,
            allowed_origin=ats.origin,
            allowed_path_pattern=APPLY_PATH_PATTERN,
            allowed_method="POST",
            allowed_stage="SUBMIT",
            application_id=application_id,
            submission_intent_id=intent_id,
        )

    runtime = loop_runtime(
        settings,
        allow_advance=True,
        max_cycles=8,
        # As substituicoes descrevem o DESTINO controlado: qual ATS esta
        # naquele endereco, como navegar ate ele e qual escrita e permitida.
        # Nenhuma delas toca material, curriculo, respostas ou snapshot.
        adapter_resolver=lambda job: GreenhouseAdapter(),
        session_factory=session_factory,
        policy_factory=policy_factory,
        allow_insecure_destination=True,
    )
    # O endereco do POST tambem e parte do destino controlado: em producao ele
    # vem de `providers.submit_destination` (Workable, Greenhouse SPA, Lever) e
    # num loopback nao existe. Mesmo padrao do `answer_provider` abaixo.
    object.__setattr__(runtime, "submission_destination", lambda job, adapter, form: ats.apply_url)
    # O gerador deterministico no lugar do LLM: sem chave configurada o produto
    # usa este mesmo caminho, e ele nao inventa fato.
    object.__setattr__(runtime, "answer_provider", GroundedTemplateProvider())
    return runtime


@pytest.mark.skipif(not LIBREOFFICE_AVAILABLE, reason="PDF real exige LibreOffice")
def test_production_runtime_generates_the_resume_and_submits_it(tmp_path: Path):
    settings = _settings(tmp_path)
    sessions: list = []
    guards: list = []

    with MultiStepATS(scenario="happy") as ats:
        # -- PROD-01/PROD-02: a vaga e a UNICA entidade inicial ----------------
        payload = json.loads(JOB_FIXTURE.read_text(encoding="utf-8"))
        ingested = ingest(settings, payload, "")
        job_id = ingested["job_id"]

        # A unica coisa ajustada: o destino e o ATS controlado (loopback), que o
        # teste precisa conhecer. Nada mais da vaga e tocado.
        database = Database(settings.resolve(settings.db_path))
        job = database.get_job(job_id)
        job.url = ats.apply_url
        database.save_job(job, f"greenhouse:{JOB_ID}", dict(payload))
        database.close()

        assert not (settings.resolve(settings.artifacts_dir) / job_id / "resume.pdf").exists()

        runtime = _runtime_for_controlled_ats(settings, ats, sessions, guards)
        database = Database(settings.resolve(settings.db_path))
        result = ApplicationLoop(database, runtime).run(job_id, submit=True)

        assert result.status == "SUBMITTED", (result.status, result.reason, result.to_dict())
        assert result.state is ApplicationState.SUBMITTED

        # -- PROD-03..PROD-08: o `prepare()` de producao rodou de verdade ------
        artifacts = settings.resolve(settings.artifacts_dir) / job_id
        generated = {
            name: artifacts / name
            for name in ("resume.txt", "resume.docx", "resume.pdf", "resume.json", "analysis.json", "fit.json", "validation.json")
        }
        for name, path in generated.items():
            assert path.is_file(), f"o pipeline nao produziu {name}"
        assert generated["resume.pdf"].stat().st_size > 0
        assert generated["resume.docx"].stat().st_size > 0

        validation = json.loads(generated["validation.json"].read_text(encoding="utf-8"))
        assert validation["valid"] is True, validation
        assert validation["facts"]["valid"] is True, validation["facts"]

        # -- o curriculo e o DA VAGA, e nao um despejo do perfil ---------------
        resume_text = generated["resume.txt"].read_text(encoding="utf-8")
        lowered = resume_text.lower()
        assert "wordpress" in lowered
        assert "woocommerce" in lowered or "php" in lowered
        assert "COBOL" not in resume_text, "o curriculo trouxe experiencia irrelevante para a vaga"
        # A vaga pede esses termos: o curriculo monta o proprio conjunto de skills.
        resume_json = json.loads(generated["resume.json"].read_text(encoding="utf-8"))
        assert resume_json["job_id"] == job_id
        assert resume_json["claims"], "curriculo sem claims"
        assert all(claim["supported_by"] for claim in resume_json["claims"]), "claim sem fato de suporte"

        # -- PROD-09/PROD-10: os MESMOS BYTES viajaram para o servidor ---------
        assert len(ats.submissions) == 1, f"esperado um POST final, veio {ats.posts}"
        received = ats.submissions[0].resume
        assert received is not None
        pdf_bytes = generated["resume.pdf"].read_bytes()
        assert received.payload == pdf_bytes, "o PDF enviado nao e o PDF gerado"
        assert hashlib.sha256(received.payload).hexdigest() == hashlib.sha256(pdf_bytes).hexdigest()
        assert result.resume_sha256 == hashlib.sha256(pdf_bytes).hexdigest()

        # -- PROD-11/PROD-12: o resolvedor respondeu, e gerou o que era aberto --
        application = database.get_application(result.application_id)
        decisions = application.context["validation"]["question_resolution"]
        generated_fields = [
            key for key, value in decisions.items() if value.get("source") == "generated_grounded"
        ]
        assert generated_fields, "nenhuma pergunta discursiva foi gerada"
        assert decisions["job_application[why_this_role]"]["source"] == "generated_grounded"
        assert decisions["job_application[why_this_role]"]["supported_by"]
        assert decisions["job_application[first_name]"]["source"].startswith("CareerProfile")

        # -- PROD-13..PROD-15: multi-step e contrato acumulado -----------------
        journey = application.context["journey"]
        assert len(journey["steps"]) == 5
        assert result.steps_completed == 4
        assert result.answers_fingerprint == journey["answers_fingerprint"]
        snapshot = database.get_review_snapshot(result.application_id)
        assert snapshot is not None
        keys = {row["key"] for row in snapshot.resolved_fields}
        for expected in (
            "job_application[first_name]",
            "job_application[experience_years]",
            "job_application[why_this_role]",
            "job_application[resume]",
            "job_application[anything_else]",
        ):
            assert expected in keys, f"o snapshot perdeu {expected}"

        # -- PROD-16/PROD-17 ---------------------------------------------------
        assert ats.posts == [ats.apply_path], "nenhuma escrita intermediaria"
        assert len(guards) == 1 and guards[0] is not None
        assert guards[0].authorized_writes_used == 1
        assert guards[0].blocked_writes == []

        # -- PROD-18/PROD-19 ---------------------------------------------------
        attempts = database.list_submission_attempts(result.application_id)
        assert len(attempts) == 1 and attempts[0].status == SUBMITTED
        assert database.get_application(result.application_id).state is ApplicationState.SUBMITTED

        # -- PROD-20: rerun nao abre browser e nao reenvia ---------------------
        again = ApplicationLoop(
            database, _runtime_for_controlled_ats(settings, ats, sessions, guards)
        ).run(job_id, submit=True)
        assert again.status == "ALREADY_SUBMITTED"
        assert len(sessions) == 1
        assert len(ats.submissions) == 1
        database.close()
