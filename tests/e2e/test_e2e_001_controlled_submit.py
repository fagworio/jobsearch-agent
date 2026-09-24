"""JSA-E2E-001: primeiro submit comprovado de ponta a ponta.

O teste não simula sucesso em nenhum ponto:

    Chromium real abre a página do ATS controlado
    → adapter real inspeciona o formulário
    → respostas vêm do perfil e das respostas já aprovadas
    → preenchimento real no DOM
    → upload real do resume.pdf
    → boundary de submissão real (snapshot, intent, autorização)
    → clique real no controle de envio
    → POST HTTP real, multipart, com o PDF
    → o SERVIDOR valida campos obrigatórios e o PDF
    → resposta 303 para a página de confirmação
    → o browser observa a confirmação
    → SubmissionAttempt = SUBMITTED e Application = SUBMITTED

As asserções que importam são as do servidor: o que chegou na rede e o que ele
aceitou. O agente não é testemunha do próprio sucesso.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# Sem Chromium instalado o arquivo é pulado, e não falha: o job core do CI não
# instala o grupo `browser`.
pytest.importorskip("playwright.sync_api", reason="E2E controlado exige Chromium real")

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.models import (
    ApplicationAnswer,
    ApplicationContext,
    ApplicationPolicy,
    ApplicationState,
    CandidatePreferences,
    CareerProfile,
    Experience,
    Job,
)
from jobsearch_agent.orchestrator import LiveApplicationOrchestrator
from jobsearch_agent.persistence import Database
from jobsearch_agent.qa import AnswerKnowledgeBase
from jobsearch_agent.submission import (
    LiveNetworkPolicy,
    SubmissionService,
    build_review_snapshot,
    compute_answers_fingerprint,
    review_field_rows,
)
from jobsearch_agent.submission_browser import BrowserSubmitter
from tests.e2e.controlled_ats import JOB_ID, ControlledATS
from tests.e2e.session import LoopbackSession

APPLY_PATH_PATTERN = r"^/jobs/e2e-001/apply$"


def _resume_pdf(path: Path) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)
    return path.read_bytes()


def _candidate() -> CareerProfile:
    """Perfil sintético e explícito: nada vem do perfil local do candidato."""
    return CareerProfile(
        identity={
            "name": "E2E Candidate",
            "first_name": "E2E",
            "last_name": "Candidate",
            "email": "e2e.candidate@example.invalid",
            "phone": "+55 11 90000-0000",
            "location": "São Paulo, Brazil",
            "current_location": "São Paulo, Brazil",
            "country": "Brazil",
            "linkedin": "https://www.linkedin.com/in/e2e-candidate",
            "github": "https://github.com/e2e-candidate",
        },
        professional_summary={
            "en-US": "WordPress developer with custom plugin and WooCommerce experience."
        },
        experiences=[
            Experience(id="exp-e2e-1", company="Example Studio", role="WordPress Developer", start_date="2020-01")
        ],
        skills={
            "wordpress": {"years": "5", "level": "advanced", "tags": ["wordpress", "php"]},
            "woocommerce": {"years": "4", "level": "advanced", "tags": ["woocommerce"]},
        },
        languages={"portuguese": {"level": "native"}, "english": {"level": "advanced"}},
        demo=False,
    )


def _approved_answers() -> AnswerKnowledgeBase:
    """Respostas que o candidato JÁ aprovou, exatamente como o domínio as lê.

    São perguntas que não saem de fato objetivo (anos de experiência, pretensão,
    consentimento) ou que exigem texto próprio — o motor unificado de respostas
    é o JSA-QA-001; aqui elas vêm do caminho real "resposta aprovada".
    """
    rows = [
        # Autorizacao de trabalho e sponsorship NAO entram aqui: eles vêm das
        # preferências do candidato, que são a fonte canônica. A pretensão, os
        # anos e a pergunta aberta são respostas que o candidato aprovou.
        ("Years of experience with WordPress", "5"),
        ("Salary expectation", "USD 3000 per month"),
        ("How did you hear about this job?", "LinkedIn"),
        (
            "Why are you interested in this role?",
            "I build WordPress products and want to keep working on the platform.",
        ),
        ("I agree to the processing of my personal data", "Yes"),
    ]
    return AnswerKnowledgeBase(
        [
            ApplicationAnswer(
                question_key=f"q-{index:02d}",
                question=question,
                answer=answer,
                source="approved_answer",
                confidence=1.0,
                approved=True,
            )
            for index, (question, answer) in enumerate(rows, 1)
        ]
    )


def _seed_application(database: Database, apply_url: str) -> tuple[str, str]:
    job = Job(
        id="job-e2e-001",
        source="greenhouse",
        external_id=JOB_ID,
        company="Controlled ATS",
        title="WordPress Developer",
        description="Build WordPress sites and plugins.",
        url=apply_url,
    )
    database.save_job(job, f"greenhouse:{JOB_ID}", {})
    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state in (
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
    ):
        application = service.transition(application.id, state, f"to_{state.value.casefold()}")
    return application.id, job.id


def test_e2e_001_controlled_ats_accepts_a_real_browser_submission(tmp_path: Path):
    database = Database(tmp_path / "e2e.db")
    resume_path = tmp_path / "resume.pdf"
    resume_bytes = _resume_pdf(resume_path)
    resume_sha256 = hashlib.sha256(resume_bytes).hexdigest()

    with ControlledATS() as ats:
        application_id, job_id = _seed_application(database, ats.apply_url)
        session = LoopbackSession(ats.origin)
        session.start()
        try:
            context = ApplicationContext(
                application_id=application_id,
                job_id=job_id,
                fit={"blockers": []},
                validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
                policy=ApplicationPolicy(autonomy={"fill_forms": "auto", "submit": "auto"}),
            )
            orchestrator = LiveApplicationOrchestrator(
                _greenhouse_adapter(),
                _candidate(),
                CandidatePreferences(
                    remote=True,
                    work_authorization=["Brazil"],
                    allowed_countries=["Brazil"],
                    requires_sponsorship="no",
                ),
                _approved_answers(),
                artifact_root=str(tmp_path),
                default_resume=str(resume_path),
                allow_advance=False,
            )
            filled = orchestrator.run(session, context, ats.apply_url)

            # 1. Preenchimento e upload aconteceram de verdade, no DOM.
            assert filled.status == "FILLED_REVIEW_REQUIRED", filled.error
            assert filled.form is not None and filled.form_fingerprint
            assert session.page.locator("#first_name").input_value() == "E2E"
            assert session.page.locator("#last_name").input_value() == "Candidate"
            assert session.page.locator("#email").input_value() == "e2e.candidate@example.invalid"
            assert session.page.locator("#experience_years").input_value() == "5"
            assert session.page.locator("#consent").is_checked() is True
            assert session.page.locator("#resume").evaluate("element => element.files.length") == 1
            assert ats.submissions == [], "nada pode ser enviado durante o preenchimento"

            # 2. Boundary de submissão real: snapshot, intent e autorização.
            answers_fingerprint = compute_answers_fingerprint(filled.form)
            resolved_fields, manual_questions = review_field_rows(filled.form)
            submission = SubmissionService(database)
            submission.save_review_snapshot(
                build_review_snapshot(
                    application_id=application_id,
                    job_id=job_id,
                    company="Controlled ATS",
                    title="WordPress Developer",
                    provider="greenhouse",
                    destination=ats.apply_url,
                    resume_filename="resume.pdf",
                    resume_sha256=resume_sha256,
                    form_fingerprint=filled.form_fingerprint,
                    answers_fingerprint=answers_fingerprint,
                    resolved_fields=resolved_fields,
                    manual_questions=manual_questions,
                )
            )
            intent = submission.create_intent(
                application_id=application_id,
                job_id=job_id,
                provider="greenhouse",
                destination=ats.apply_url,
                form_fingerprint=filled.form_fingerprint,
                resume_sha256=resume_sha256,
                answers_fingerprint=answers_fingerprint,
                expires_in_seconds=300,
                allow_insecure_destination=True,
            )
            submission.authorize_submission(intent.id)

            # 3. O clique e o POST, com um único permit de escrita.
            outcome = BrowserSubmitter(database, timeout_seconds=30.0).submit(
                session,
                intent.id,
                current_form_fingerprint=filled.form_fingerprint,
                current_resume_sha256=resume_sha256,
                current_answers_fingerprint=answers_fingerprint,
                policy=LiveNetworkPolicy(
                    provider="greenhouse",
                    allowed_origin=ats.origin,
                    allowed_path_pattern=APPLY_PATH_PATTERN,
                    allowed_method="POST",
                    allowed_stage="SUBMIT",
                    application_id=application_id,
                    submission_intent_id=intent.id,
                ),
            )

            # 4. O SERVIDOR: um POST, multipart, PDF válido, campos completos.
            assert outcome.status == "SUBMITTED", (outcome.status, outcome.error, outcome.evidence)
            assert len(ats.submissions) == 1, f"esperado exatamente um POST, veio {ats.posts}"
            received = ats.submissions[0]
            assert received.path == ats.apply_path
            assert received.is_multipart is True
            assert received.missing_required() == []

            resume = received.resume
            assert resume is not None, "o POST não carregou o currículo"
            assert resume.filename.endswith(".pdf")
            assert resume.payload.startswith(b"%PDF-")
            assert hashlib.sha256(resume.payload).hexdigest() == resume_sha256, "o PDF recebido não é o aprovado"

            fields = received.fields()
            assert fields["job_application[first_name]"] == "E2E"
            assert fields["job_application[last_name]"] == "Candidate"
            assert fields["job_application[email]"] == "e2e.candidate@example.invalid"
            assert fields["job_application[phone]"] == "+55 11 90000-0000"
            assert fields["job_application[location]"] == "São Paulo, Brazil"
            assert fields["job_application[linkedin]"] == "https://www.linkedin.com/in/e2e-candidate"
            assert fields["job_application[github]"] == "https://github.com/e2e-candidate"
            assert fields["job_application[experience_years]"] == "5"
            assert fields["job_application[salary_expectation]"]
            assert fields["job_application[authorized_to_work_in_brazil]"] == "Yes"
            assert fields["job_application[requires_sponsorship]"] == "No"
            assert fields["job_application[source]"] == "LinkedIn"
            assert fields["job_application[why_this_role]"].strip()
            assert "consent" in "".join(received.fields())

            # 5. Estado: tentativa e Application terminaram em SUBMITTED.
            attempts = database.list_submission_attempts(application_id)
            assert len(attempts) == 1
            assert attempts[0].status == ApplicationState.SUBMITTED.value
            assert database.get_application(application_id).state is ApplicationState.SUBMITTED

            # 6. Uma escrita autorizada, nenhuma bloqueada.
            assert session.network_guard is not None
            assert session.network_guard.authorized_writes_used == 1
            assert session.network_guard.blocked_writes == []
        finally:
            session.close()
            database.close()


def _greenhouse_adapter():
    from jobsearch_agent.ats import GreenhouseAdapter

    return GreenhouseAdapter()
