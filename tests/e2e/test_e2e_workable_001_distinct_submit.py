"""JSA-AUTONOMY-CORE-001 + WORKABLE-AUTO-CERT-001: formulario != submit.

O `ApplicationLoop` mandava a candidatura para a **URL do formulario**. Em ATS
onde os dois enderecos coincidem (Lever, Greenhouse moderno) isso passava
despercebido; no Workable nao:

    formulario   /pavago/j/711D24E5DB/apply
    submit       /api/v1/accounts/pavago/jobs/711D24E5DB/applications

Este teste conduz o loop ate `SUBMITTED` contra um ATS controlado com esse
formato e prova, do lado do SERVIDOR:

    exatamente um POST, no endpoint de candidatura
    a pagina do formulario NAO recebeu escrita nenhuma
    o PDF que chegou e o PDF preparado (byte a byte)

O que fica injetado e so o DESTINO controlado (qual adapter responde por aquele
endereco, a sessao de loopback, a policy restrita e o endereco de submit do
servidor de teste). Perfil, respostas, material, SHA, snapshot e intent vem do
caminho de producao do loop.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="E2E exige Chromium real")

from jobsearch_agent.ats import WorkableAdapter
from jobsearch_agent.loop import ApplicationLoop, LoopRuntime, PreparedMaterial
from jobsearch_agent.models import (
    ApplicationAnswer,
    ApplicationState,
    CandidatePreferences,
    CareerProfile,
    Experience,
    Job,
)
from jobsearch_agent.persistence import Database
from jobsearch_agent.qa import AnswerKnowledgeBase
from jobsearch_agent.resolver import GroundedTemplateProvider
from jobsearch_agent.submission import LiveNetworkPolicy
from tests.e2e.session import LoopbackSession
from tests.e2e.workable_ats import ACCOUNT, JOB_ID, SHORTCODE, WorkableATS

SUBMITTED = ApplicationState.SUBMITTED.value


def _resume_pdf(path: Path) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)
    return path.read_bytes()


def _candidate() -> CareerProfile:
    return CareerProfile(
        identity={
            "name": "Workable Candidate",
            "first_name": "Workable",
            "last_name": "Candidate",
            "email": "workable.candidate@example.invalid",
            "phone": "+55 11 90000-0000",
            "current_location": "São Paulo, Brazil",
            "country": "Brazil",
            "linkedin": "https://www.linkedin.com/in/workable-candidate",
        },
        professional_summary={"en-US": "WordPress developer with plugin experience."},
        experiences=[Experience(id="exp-1", company="Acme", role="WordPress Developer", start_date="2019-01")],
        skills={"wordpress": {"years": "7", "level": "advanced", "tags": ["wordpress", "php"]}},
        languages={"english": {"level": "advanced"}},
        demo=False,
    )


def _preferences() -> CandidatePreferences:
    return CandidatePreferences(work_authorization=["Brazil"], requires_sponsorship="no")


def _answers() -> AnswerKnowledgeBase:
    """Nenhuma resposta aprovada: o que existe e fato do perfil.

    A pergunta customizada do cenario e "LinkedIn profile", que o resolvedor
    responde pelo fato do perfil — sem geracao e sem regra.
    """
    return AnswerKnowledgeBase([])


def _runtime(ats: WorkableATS, tmp_path: Path, resume_path: Path, resume_sha256: str, sessions: list, guards: list) -> LoopRuntime:
    def open_session(job, adapter):
        session = LoopbackSession(ats.origin)
        session.start()
        sessions.append(session)
        guards.append(session.network_guard)
        return session

    def policy_for(provider, application_id, intent_id):
        # Loopback: a policy e injetada, mas continua validada contra a intent.
        # O caminho permitido e SO o endpoint de candidatura — e nao a pagina.
        return LiveNetworkPolicy(
            provider=provider,
            allowed_origin=ats.origin,
            allowed_path_pattern=r"^/api/v[0-9]+/accounts/[^/]+/jobs/[^/]+/applications/?$",
            allowed_method="POST",
            allowed_stage="SUBMIT",
            application_id=application_id,
            submission_intent_id=intent_id,
        )

    return LoopRuntime(
        adapter_for=lambda job: WorkableAdapter(),
        # A URL do FORMULARIO...
        form_url=lambda job, adapter: ats.apply_url,
        # ...e o endereco que recebe o POST, que aqui e OUTRO (o hook de
        # AUTONOMY-CORE-001; em producao quem responde e `providers.submit_destination`).
        submission_destination=lambda job, adapter, form: ats.submit_url,
        profile=_candidate(),
        preferences=_preferences(),
        answers=_answers(),
        prepare=lambda job: PreparedMaterial(
            resume_path=str(resume_path),
            resume_sha256=resume_sha256,
            artifact_root=str(tmp_path),
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        ),
        open_session=open_session,
        policy_for=policy_for,
        answer_provider=GroundedTemplateProvider(),
        allow_insecure_destination=True,
        allow_advance=False,
    )


def _seed_job(database: Database, job_url: str) -> str:
    job = Job(
        id="job-workable-001",
        source="workable",
        external_id=SHORTCODE,
        company=ACCOUNT,
        title="WordPress Developer",
        description="Build WordPress sites and plugins.",
        url=job_url,
    )
    database.save_job(job, f"workable:{SHORTCODE}", {})
    return job.id


def test_the_loop_submits_to_the_submit_endpoint_and_not_to_the_form_page(tmp_path: Path):
    database = Database(tmp_path / "workable.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(_resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    with WorkableATS() as ats:
        job_id = _seed_job(database, ats.apply_url)
        result = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)

        # 1. O contrato do loop.
        assert result.status == SUBMITTED, (result.status, result.reason, result.to_dict())
        assert result.state is ApplicationState.SUBMITTED
        assert result.submission_attempted is True
        assert result.submission_writes == 1
        # Workable nao tem upload pre-submit: o multipart E a submissao.
        assert result.upload_writes_used == 0

        # 2. O SERVIDOR prova para onde o POST foi.
        assert ats.posts == [ats.submit_path], f"POST foi para {ats.posts}"
        assert len(ats.submissions) == 1
        received = ats.submissions[0]
        assert received.is_multipart
        assert received.field("firstname") == "Workable"
        assert received.field("email") == "workable.candidate@example.invalid"
        assert received.fields().get("QA_1", "").startswith("https://www.linkedin.com/in/")
        assert received.resume is not None
        assert received.resume.payload == resume_path.read_bytes()
        assert hashlib.sha256(received.resume.payload).hexdigest() == resume_sha256

        # 3. A pagina do formulario nao recebeu escrita nenhuma.
        assert ats.apply_path not in ats.posts

        # 4. A intent aponta para o endpoint de candidatura, nao para a pagina.
        snapshot = database.get_review_snapshot(result.application_id)
        assert snapshot is not None
        assert snapshot.destination == ats.submit_url
        assert snapshot.destination != ats.apply_url

        # 5. O guard cobrou exatamente uma escrita, no caminho permitido.
        assert len(guards) == 1 and guards[0] is not None
        assert guards[0].authorized_writes_used == 1
        assert guards[0].blocked_writes == []

        attempts = database.list_submission_attempts(result.application_id)
        assert len(attempts) == 1 and attempts[0].status == SUBMITTED

        # 6. Rerun nao abre browser e nao reenvia.
        again = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)
        assert again.status == "ALREADY_SUBMITTED"
        assert len(sessions) == 1
        assert ats.posts == [ats.submit_path]
        database.close()
