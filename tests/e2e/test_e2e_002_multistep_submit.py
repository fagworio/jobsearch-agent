"""JSA-E2E-002 / JSA-LOOP-002: candidatura multi-step ate `SUBMITTED`.

Cinco telas, DOM substituido a cada avanco, e a autorizacao final precisa cobrir
o TODO — nao a ultima tela. O teste nao monta etapa nenhuma: entrega `job_id` e
o runtime ao `ApplicationLoop`.

O que ele mede:

    step 1 contato   -> step 2 experiencia -> step 3 perguntas (2 geradas)
    -> step 4 artefatos -> step 5 review -> SUBMIT
    -> 1 POST com TODOS os campos -> SUBMITTED

E o que ele recusa: repeticao de tela, estouro de ciclos e pergunta factual sem
fato param o fluxo com ZERO submissao.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

# Sem Chromium instalado o arquivo e pulado, e nao falha.
pytest.importorskip("playwright.sync_api", reason="E2E multi-step exige Chromium real")

from jobsearch_agent.ats import GreenhouseAdapter
from jobsearch_agent.journey import ApplicationJourney
from jobsearch_agent.loop import ApplicationLoop, LoopRuntime, PreparedMaterial
from jobsearch_agent.models import ApplicationAnswer, ApplicationState
from jobsearch_agent.persistence import Database
from jobsearch_agent.qa import AnswerKnowledgeBase
from jobsearch_agent.resolver import GroundedTemplateProvider
from jobsearch_agent.submission import LiveNetworkPolicy, compute_answers_fingerprint
from tests.e2e.multistep_ats import JOB_ID, MultiStepATS, required_fields
from tests.e2e.session import LoopbackSession
from tests.e2e.test_e2e_001_controlled_submit import _candidate, _preferences

APPLY_PATH_PATTERN = r"^/jobs/e2e-002/apply$"
SUBMITTED = ApplicationState.SUBMITTED.value


def _resume_pdf(path: Path) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)
    return path.read_bytes()


def _approved_answers() -> AnswerKnowledgeBase:
    """Aberto NAO entra: `why_this_role` e `relevant_project` sao gerados."""
    rows = [
        ("Years of experience with WordPress", "5"),
        ("Salary expectation", "USD 3000 per month"),
        ("How did you hear about this job?", "LinkedIn"),
        ("I agree to the processing of my personal data", "Yes"),
    ]
    return AnswerKnowledgeBase(
        [
            ApplicationAnswer(
                question_key=f"ms-{index:02d}",
                question=question,
                answer=answer,
                source="approved_answer",
                confidence=1.0,
                approved=True,
            )
            for index, (question, answer) in enumerate(rows, 1)
        ]
    )


def _seed_job(database: Database, apply_url: str) -> str:
    from jobsearch_agent.models import Job

    job = Job(
        id="job-e2e-002",
        source="greenhouse",
        external_id=JOB_ID,
        company="Controlled MultiStep ATS",
        title="WordPress Developer",
        description="Build WordPress sites and plugins.",
        url=apply_url,
        language="en-US",
    )
    database.save_job(job, f"greenhouse:{JOB_ID}", {})
    return job.id


def _runtime(
    ats: MultiStepATS,
    tmp_path: Path,
    resume_path: Path,
    resume_sha256: str,
    sessions: list,
    guards: list,
    *,
    max_cycles: int = 5,
) -> LoopRuntime:
    def open_session(job, adapter):
        session = LoopbackSession(ats.origin)
        session.start()
        sessions.append(session)
        guards.append(session.network_guard)
        return session

    def policy_for(provider, application_id, intent_id):
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
        answer_provider=GroundedTemplateProvider(),
        policy_for=policy_for,
        allow_insecure_destination=True,
        allow_advance=True,
        max_cycles=max_cycles,
    )


def test_loop_completes_a_multistep_application(tmp_path: Path):
    database = Database(tmp_path / "e2e-002.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(_resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    with MultiStepATS(scenario="happy") as ats:
        job_id = _seed_job(database, ats.apply_url)
        result = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)

        # -- MS-01..MS-07: o loop atravessou as cinco telas ---------------------
        assert result.status == "SUBMITTED", (result.status, result.reason, result.to_dict())
        assert result.state is ApplicationState.SUBMITTED
        assert result.terminal is True
        assert result.cycles == 5, "cinco telas inspecionadas"
        assert result.steps_completed == 4, "quatro avancos concluidos"
        assert result.questions_answered == 13, (
            "o total vem do CONTRATO acumulado: 4 contato + 4 experiencia + 3 perguntas + 2 artefatos"
        )
        assert result.unanswered_required == ()
        assert result.submission_attempted is True

        # -- MS-07/MS-08/MS-10: o contrato acumulado, persistido e auditavel ---
        application = database.get_application(result.application_id)
        journey = application.context["journey"]
        assert len(journey["steps"]) == 5
        fingerprints = [step["form_fingerprint"] for step in journey["steps"]]
        assert len(set(fingerprints)) == 5, "cada tela tem o proprio fingerprint"
        assert journey["answers_fingerprint"] == result.answers_fingerprint

        decisions = application.context["validation"]["question_resolution"]
        assert decisions["job_application[first_name]"]["step"] == 1
        assert decisions["job_application[why_this_role]"]["step"] == 3
        assert decisions["job_application[why_this_role]"]["source"] == "generated_grounded"
        assert decisions["job_application[relevant_project]"]["source"] == "generated_grounded"
        # Proveniencia do passo 1 continua auditavel no fim do fluxo.
        assert decisions["job_application[first_name]"]["source"].startswith("CareerProfile")
        assert "CareerProfile.identity.first_name" in decisions["job_application[first_name]"]["supported_by"]
        assert decisions["job_application[why_this_role]"]["supported_by"], "geracao sem proveniencia"

        # MS-12: o form_fingerprint e a superficie FINAL.
        assert result.form_fingerprint == fingerprints[-1]
        assert result.form_fingerprint != fingerprints[0]

        # MS-10: o answers_fingerprint cobre tudo, e nao a ultima tela.
        final_form = database.get_application_form(result.application_id)
        last_step_only = compute_answers_fingerprint(
            GreenhouseAdapter().inspect(ats.page(), ats.apply_url).form
        )
        assert final_form is not None
        assert result.answers_fingerprint != last_step_only, "o fingerprint nao pode ser so o da tela final"
        assert result.answers_fingerprint == journey["answers_fingerprint"]

        # MS-11: o ReviewSnapshot cobre TODAS as etapas.
        snapshot = database.get_review_snapshot(result.application_id)
        assert snapshot is not None
        keys = {row["key"] for row in snapshot.resolved_fields}
        for expected in (
            "job_application[first_name]",      # step 1
            "job_application[experience_years]",  # step 2
            "job_application[why_this_role]",     # step 3
            "job_application[resume]",            # step 4
            "job_application[anything_else]",     # step 5
        ):
            assert expected in keys, f"o snapshot perdeu {expected}"
        assert snapshot.answers_fingerprint == result.answers_fingerprint
        assert snapshot.form_fingerprint == result.form_fingerprint

        # -- MS-13/MS-14: o SERVIDOR recebeu tudo ------------------------------
        assert len(ats.submissions) == 1, f"esperado um POST final, veio {ats.posts}"
        assert ats.posts == [ats.apply_path], "nenhuma escrita intermediaria"
        received = ats.submissions[0]
        assert received.is_multipart is True
        fields = received.fields()
        missing = [
            name
            for name in required_fields(ats.scenario)
            if name not in received.files() and not fields.get(name, "").strip()
        ]
        assert missing == [], f"o POST chegou incompleto: {missing}"

        assert fields["job_application[first_name]"] == "E2E"
        assert fields["job_application[email]"] == "e2e.candidate@example.invalid"
        assert fields["job_application[experience_years]"] == "5"
        assert fields["job_application[authorized_to_work_in_brazil]"] == "Yes"
        assert fields["job_application[requires_sponsorship]"] == "No"
        assert fields["job_application[source]"] == "LinkedIn"
        assert fields["job_application[why_this_role]"].strip()
        assert fields["job_application[relevant_project]"].strip()
        assert "consent" in "".join(fields)

        resume = received.resume
        assert resume is not None and resume.filename.endswith(".pdf")
        assert hashlib.sha256(resume.payload).hexdigest() == resume_sha256

        # -- MS-15/MS-16: um POST, uma escrita autorizada, nenhuma bloqueada ----
        assert len(guards) == 1 and guards[0] is not None
        assert guards[0].authorized_writes_used == 1
        assert guards[0].blocked_writes == []

        # -- MS-17/MS-18 -------------------------------------------------------
        attempts = database.list_submission_attempts(result.application_id)
        assert len(attempts) == 1 and attempts[0].status == SUBMITTED
        assert database.get_application(result.application_id).state is ApplicationState.SUBMITTED

        # -- MS-22: rerun nao abre browser e nao envia -------------------------
        again = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)
        assert again.status == "ALREADY_SUBMITTED"
        assert len(sessions) == 1, "o rerun abriu um browser"
        assert len(ats.submissions) == 1, "o rerun enviou de novo"

        database.close()


def test_a_repeated_step_is_detected_and_nothing_is_submitted(tmp_path: Path):
    """MS-19: `fp-A -> fp-B -> fp-A` para o fluxo, sem submit e sem escrita."""
    database = Database(tmp_path / "loop.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(_resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    with MultiStepATS(scenario="loop") as ats:
        job_id = _seed_job(database, ats.apply_url)
        result = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)

        assert result.status == "LOOP_DETECTED"
        assert result.terminal is True
        assert result.requires_action == "LOOP_DETECTED"
        assert result.submission_attempted is False
        assert result.submission_writes == 0
        assert ats.posts == [], "nenhuma escrita, nem intermediaria nem final"
        assert database.get_application(result.application_id).state is not ApplicationState.SUBMITTED
        database.close()


def test_max_cycles_stops_before_submitting(tmp_path: Path):
    """MS-20: o limite e seguranca; ele nunca produz submissao parcial."""
    database = Database(tmp_path / "loop.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(_resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    with MultiStepATS(scenario="extra") as ats:
        job_id = _seed_job(database, ats.apply_url)
        result = ApplicationLoop(
            database,
            _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards, max_cycles=5),
        ).run(job_id, submit=True)

        assert result.status == "MAX_CYCLES_EXCEEDED"
        assert result.cycles == 5
        assert result.submission_attempted is False
        assert result.submission_writes == 0
        assert ats.posts == []
        database.close()


def test_an_unanswerable_question_mid_flow_stops_there(tmp_path: Path):
    """MS-21: o sistema para no ponto exato da incerteza, sem avancar nem enviar."""
    database = Database(tmp_path / "loop.db")
    resume_path = tmp_path / "resume.pdf"
    resume_sha256 = hashlib.sha256(_resume_pdf(resume_path)).hexdigest()
    sessions: list = []
    guards: list = []

    with MultiStepATS(scenario="unknown") as ats:
        job_id = _seed_job(database, ats.apply_url)
        result = ApplicationLoop(
            database, _runtime(ats, tmp_path, resume_path, resume_sha256, sessions, guards)
        ).run(job_id, submit=True)

        assert result.status == "NEEDS_ANSWER", result.to_dict()
        assert result.terminal is False
        assert result.requires_action == ApplicationState.NEEDS_ANSWER.value
        assert result.submission_attempted is False
        assert result.submission_writes == 0
        assert result.unanswered_required == ("Notice period",)
        # Parou NA terceira tela: duas etapas concluidas, nenhuma submissao.
        assert result.steps_completed == 2
        assert result.cycles == 3
        assert ats.posts == []
        assert database.get_application(result.application_id).state is ApplicationState.NEEDS_ANSWER

        decisions = database.get_application(result.application_id).context["validation"]["question_resolution"]
        assert decisions["job_application[job_notice_period]"]["status"] == "NEEDS_HUMAN"
        assert decisions["job_application[job_notice_period]"]["reason"] == "no_verifiable_fact"
        database.close()


def test_the_journey_keeps_every_step_and_refuses_contradictions():
    """O acumulador, em isolamento: identidade estavel, primeira resposta vence."""
    from jobsearch_agent.models import ApplicationField, ApplicationForm

    journey = ApplicationJourney(provider="greenhouse", job_id="job-1")
    step_one = ApplicationForm(
        form_id="s1",
        provider="greenhouse",
        fields=[ApplicationField(key="job_application[sponsorship]", label="Sponsorship?", required=True, value="No")],
    )
    journey.add_step(index=1, form=step_one, form_fingerprint="fp-A", url="u1")
    assert journey.conflicts == ()
    assert journey.steps_completed == 0

    # Mesmo campo, mesmo valor: idempotente.
    journey.add_step(index=2, form=step_one, form_fingerprint="fp-B", url="u2")
    assert journey.conflicts == ()
    assert len(journey.resolved_fields()) == 1

    # Mesmo campo, valor diferente: contradicao, e nada de "a ultima vence".
    contradicted = ApplicationForm(
        form_id="s3",
        provider="greenhouse",
        fields=[ApplicationField(key="job_application[sponsorship]", label="Sponsorship?", required=True, value="Yes")],
    )
    journey.add_step(index=3, form=contradicted, form_fingerprint="fp-C", url="u3")
    assert len(journey.conflicts) == 1
    conflict = journey.conflicts[0]
    assert conflict.first_index == 1 and conflict.first_value == "No"
    assert conflict.later_index == 3 and conflict.later_value == "Yes"
    assert journey.resolved_fields()[0].value == "No", "a primeira resposta fica"

    as_form = journey.as_form()
    assert [field.key for field in as_form.fields] == ["job_application[sponsorship]"]
    assert journey.answers_fingerprint()
