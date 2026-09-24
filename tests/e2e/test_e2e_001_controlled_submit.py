"""JSA-E2E-001 / JSA-LOOP-001: `job-id` entra, `SUBMITTED` sai.

O teste nao monta mais etapa nenhuma. Ele entrega ao loop:

    job_id  +  um runtime injetado (adapter, sessao, material, politica)

e o loop faz o resto: prepara o material, abre o Chromium, inspeciona o
formulario, resolve as respostas, preenche, anexa o curriculo, cria e autoriza a
intent, clica no controle de envio e observa o desfecho.

O que ele NAO simula: o POST, o multipart, o PDF, a pagina de confirmacao e o
estado final. As assercoes continuam sendo do SERVIDOR — exatamente um POST,
PDF valido com o SHA aprovado, campos obrigatorios completos.

Antes desta migracao o teste importava `SubmissionService`, `BrowserSubmitter` e
`build_review_snapshot` e remontava a sequencia a mao; todas as assercoes de
servidor continuam aqui, porque perder alguma no caminho seria enfraquecer o
teste em silencio.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# Sem Chromium instalado o arquivo é pulado, e não falha: o job core do CI não
# instala o grupo `browser`.
pytest.importorskip("playwright.sync_api", reason="E2E controlado exige Chromium real")

from jobsearch_agent.ats import GreenhouseAdapter
from jobsearch_agent.loop import ApplicationLoop, LoopPhase, LoopRuntime, PreparedMaterial
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
from jobsearch_agent.submission import LiveNetworkPolicy
from tests.e2e.controlled_ats import JOB_ID, ControlledATS
from tests.e2e.session import LoopbackSession

APPLY_PATH_PATTERN = r"^/jobs/e2e-001/apply$"
SUBMITTED = ApplicationState.SUBMITTED.value


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


def _preferences() -> CandidatePreferences:
    return CandidatePreferences(
        remote=True,
        work_authorization=["Brazil"],
        allowed_countries=["Brazil"],
        requires_sponsorship="no",
    )


def _approved_answers() -> AnswerKnowledgeBase:
    """Respostas já aprovadas pelo candidato.

    Autorização de trabalho e sponsorship NÃO entram aqui: vêm das preferências,
    que são a fonte canônica. O motor unificado de respostas é o JSA-QA-001.
    """
    rows = [
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


def _runtime(
    ats: ControlledATS,
    tmp_path: Path,
    resume_path: Path,
    resume_sha256: str,
    sessions: list,
    guards: list,
) -> LoopRuntime:
    def open_session(job, adapter):
        session = LoopbackSession(ats.origin)
        session.start()
        sessions.append(session)
        # O loop fecha a sessao no `finally` (correto) e `close()` descarta a
        # referencia ao guard: guardar o OBJETO permite auditar o orcamento
        # depois do fato, que e o ponto de LOOP-15.
        guards.append(session.network_guard)
        return session

    def policy_for(provider, application_id, intent_id):
        # A construcao da politica e injetada porque o endpoint e loopback; ela
        # continua sendo validada contra a intent dentro de `begin_submission`.
        return LiveNetworkPolicy(
            provider=provider,
            allowed_origin=ats.origin,
            allowed_path_pattern=APPLY_PATH_PATTERN,
            allowed_method="POST",
            allowed_stage="SUBMIT",
            application_id=application_id,
            submission_intent_id=intent_id,
        )

    return LoopRuntime(
        adapter_for=lambda job: GreenhouseAdapter(),
        form_url=lambda job, adapter: ats.apply_url,
        profile=_candidate(),
        preferences=_preferences(),
        answers=_approved_answers(),
        prepare=lambda job: PreparedMaterial(
            resume_path=str(resume_path),
            resume_sha256=resume_sha256,
            artifact_root=str(tmp_path),
            validation={"valid": True, "facts": {"valid": True}, "ats": {"valid": True}},
        ),
        open_session=open_session,
        policy_for=policy_for,
        allow_insecure_destination=True,
        allow_advance=False,
    )


def _seed_job(database: Database, apply_url: str) -> str:
    """Só a vaga. A Application e todo o resto ficam com o loop (LOOP-01)."""
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
    return job.id


def test_loop_takes_a_job_id_to_submitted_without_manual_steps(tmp_path: Path):
    database = Database(tmp_path / "e2e.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(_resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    with ControlledATS() as ats:
        job_id = _seed_job(database, ats.apply_url)
        result = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)

        # 1. O contrato do loop: job-id entrou, desfecho saiu.
        assert result.status == "SUBMITTED", (result.status, result.reason, result.to_dict())
        assert result.terminal is True
        assert result.requires_action == ""
        assert result.state is ApplicationState.SUBMITTED
        assert result.submission_attempted is True
        assert result.submission_writes == 1
        assert result.resume_sha256 == resume_sha256
        assert result.form_fingerprint and result.answers_fingerprint
        assert result.unanswered_required == ()
        assert result.questions_answered >= 15, result.questions_answered
        assert set(result.phases) >= {
            LoopPhase.PREPARE.value,
            LoopPhase.INSPECT.value,
            LoopPhase.RESOLVE.value,
            LoopPhase.FILL.value,
            LoopPhase.SUBMIT.value,
            LoopPhase.OBSERVE.value,
        }

        # 2. O SERVIDOR: um POST, multipart, PDF válido, campos completos.
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
        assert "consent" in "".join(fields)

        # 3. Estado persistido: tentativa e Application em SUBMITTED.
        attempts = database.list_submission_attempts(result.application_id)
        assert len(attempts) == 1
        assert attempts[0].status == SUBMITTED
        assert database.get_application(result.application_id).state is ApplicationState.SUBMITTED

        # 4. Uma escrita autorizada, nenhuma bloqueada.
        assert len(sessions) == 1
        guard = guards[0]
        assert guard is not None
        assert guard.authorized_writes_used == 1
        assert guard.blocked_writes == []

        # 5. LOOP-16: rodar de novo não abre browser e não envia nada.
        again = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)
        assert again.status == "ALREADY_SUBMITTED"
        assert again.terminal is True
        assert again.submission_attempted is False
        assert len(sessions) == 1, "o rerun abriu um browser"
        assert len(ats.submissions) == 1, "o rerun enviou de novo"

        database.close()
