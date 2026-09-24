"""JSA-CG-016: pacote de handoff humano — atomicidade e imutabilidade.

Os testes que importam sao os que provam que o ESTADO nao anda quando o pacote
nao pode ser produzido, e que o pacote nao muda quando o material muda depois.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.cli import main as cli_main
from jobsearch_agent.handoff import (
    HandoffError,
    HumanHandoffPackage,
    HumanHandoffService,
)
from jobsearch_agent.models import (
    ApplicationAnswer,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    Job,
)
from jobsearch_agent.persistence import Database
from jobsearch_agent.submission import (
    LiveNetworkPolicy,
    SubmissionService,
    SubmissionVerification,
    build_review_snapshot,
    compute_answers_fingerprint,
)

#: Valores sentinela: se aparecerem no journal de eventos, algo vazou.
APPROVED_ANSWER = "sentinel-approved-9f3a"
UNAPPROVED_ANSWER = "sentinel-draft-4b71"
CANDIDATE_EMAIL = "sentinel.candidate@example.invalid"

DESTINATION = "https://jobs.lever.co/pavago/7f3a91c2-1e4b-4d5a-9c8f-2b6e5a1d0f33/apply"
FORM_FINGERPRINT = "f" * 64
CHALLENGE = {"provider": "hcaptcha", "reason_token": "provider_rejected_submission", "session_id": "cs-abc123"}


@dataclass
class World:
    database: Database
    artifacts: Path
    application_id: str
    job_id: str
    resume: Path
    package_dir: Path
    service: HumanHandoffService


def _form() -> ApplicationForm:
    approved = ApplicationAnswer(
        question_key="why-this-role",
        question="Why do you want this role?",
        answer=APPROVED_ANSWER,
        supported_by=["answer_policy:why"],
        source="approved_answer",
        confidence=1.0,
        approved=True,
    )
    draft = ApplicationAnswer(
        question_key="draft-question",
        question="Anything else?",
        answer=UNAPPROVED_ANSWER,
        source="agent_draft",
        confidence=0.4,
        approved=False,
    )
    return ApplicationForm(
        form_id="form-handoff",
        provider="lever",
        fields=[
            ApplicationField(key="email", label="Email", field_type="email", semantic_type="email", required=True, value=CANDIDATE_EMAIL, answer=ApplicationAnswer(question_key="email", question="Email", answer=CANDIDATE_EMAIL, source="CareerProfile", confidence=1.0, approved=True, semantic_type="email"), confidence=1.0, source="CareerProfile"),
            ApplicationField(key="why", label="Why do you want this role?", field_type="textarea", semantic_type="unknown", required=True, value=APPROVED_ANSWER, answer=approved, confidence=1.0, source="AnswerPolicy"),
            ApplicationField(key="other", label="Anything else?", field_type="textarea", semantic_type="unknown", required=False, value=UNAPPROVED_ANSWER, answer=draft, confidence=0.4, source="agent_draft"),
        ],
        source="live",
        artifact_root="",
    )


def _build(tmp_path: Path, suffix: str = "hop", *, challenge: dict | None = CHALLENGE) -> World:
    database = Database(tmp_path / f"{suffix}.db")
    job = Job(
        id=f"job-{suffix}",
        source="lever",
        external_id="7f3a91c2-1e4b-4d5a-9c8f-2b6e5a1d0f33",
        company="Pavago",
        title="WordPress Developer",
        description="Build WordPress sites",
        url="https://jobs.lever.co/pavago/7f3a91c2-1e4b-4d5a-9c8f-2b6e5a1d0f33",
    )
    database.save_job(job, f"lever:{suffix}", {})
    artifacts = tmp_path / "applications"
    resume = artifacts / job.id / "resume.pdf"
    resume.parent.mkdir(parents=True, exist_ok=True)
    resume.write_bytes(b"%PDF-1.4 approved resume")

    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state in (
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
    ):
        application = service.transition(application.id, state, f"to_{state.value.casefold()}")
    resume_sha256 = hashlib.sha256(resume.read_bytes()).hexdigest()
    application.context["resume_sha256"] = resume_sha256
    # O fluxo vivo guarda as respostas resolvidas no contexto da Application.
    # Reproduzir isso aqui e o que da sentido ao teste de vazamento: sem elas,
    # imprimir a Application inteira nao vazaria nada.
    application.context["answers"] = [
        {"question": "Why do you want this role?", "answer": APPROVED_ANSWER, "approved": True},
        {"question": "Email", "answer": CANDIDATE_EMAIL, "approved": True},
    ]
    database.save_application(application)

    form = _form()
    database.save_application_form(application.id, form)
    answers_fingerprint = compute_answers_fingerprint(form)

    submission = SubmissionService(database)
    intent = submission.create_intent(
        application_id=application.id,
        job_id=job.id,
        provider="lever",
        destination=DESTINATION,
        form_fingerprint=FORM_FINGERPRINT,
        resume_sha256=resume_sha256,
        answers_fingerprint=answers_fingerprint,
        expires_in_seconds=300,
    )
    submission.save_review_snapshot(
        build_review_snapshot(
            application_id=application.id,
            job_id=job.id,
            company=job.company,
            title=job.title,
            provider="lever",
            destination=DESTINATION,
            resume_filename="resume.pdf",
            resume_sha256=resume_sha256,
            form_fingerprint=FORM_FINGERPRINT,
            answers_fingerprint=answers_fingerprint,
        )
    )
    submission.authorize_submission(intent.id)
    attempt = submission.begin_submission(
        intent.id,
        current_form_fingerprint=FORM_FINGERPRINT,
        current_resume_sha256=resume_sha256,
        current_answers_fingerprint=answers_fingerprint,
        policy=LiveNetworkPolicy.for_submission("lever", application.id, intent.id),
        method="POST",
        url=DESTINATION,
    )
    submission.record_result(
        attempt.id,
        SubmissionVerification.challenged(
            "captcha_verification_failed",
            http_status=400,
            submit_write=True,
            challenge=challenge,
        ),
    )
    assert database.get_application(application.id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    return World(
        database,
        artifacts,
        application.id,
        job.id,
        resume,
        artifacts / job.id / "handoff",
        HumanHandoffService(database, artifacts),
    )


# --- o caminho feliz ----------------------------------------------------------


def test_the_package_is_built_and_the_state_moves(tmp_path):
    world = _build(tmp_path)
    package = world.service.prepare_handoff(world.application_id)

    assert world.database.get_application(world.application_id).state is ApplicationState.HANDOFF_IN_PROGRESS
    assert package.resume_sha256 == hashlib.sha256(world.resume.read_bytes()).hexdigest()
    assert package.destination == DESTINATION
    assert package.provider == "hcaptcha"
    assert package.reason_token == "provider_rejected_submission"
    assert package.challenge_session_id == "cs-abc123"
    assert package.continuation == "manual_final"
    assert package.instructions, "o humano precisa da instrucao, que vem do challenge-guard"
    assert package.answers_fingerprint
    assert {row.question_key: row.answer for row in package.approved_answers} == {
        "email": CANDIDATE_EMAIL,
        "why-this-role": APPROVED_ANSWER,
    }
    world.database.close()


def test_the_bundle_is_self_contained_and_matches_the_record(tmp_path):
    world = _build(tmp_path)
    package = world.service.prepare_handoff(world.application_id)

    bundle = world.artifacts / package.package_path
    resume_copy = world.artifacts / package.resume_path
    assert bundle.is_file() and resume_copy.is_file()
    assert resume_copy.read_bytes() == world.resume.read_bytes()
    stored = json.loads(bundle.read_text(encoding="utf-8"))
    assert stored["package_sha256"] == package.package_sha256
    assert HumanHandoffPackage.from_dict(stored).package_sha256 == package.package_sha256
    assert world.service.load_package(package.package_id).to_dict() == package.to_dict()
    world.database.close()


def test_the_cli_command_is_a_thin_shell_over_the_service(tmp_path, capsys):
    world = _build(tmp_path)
    code = cli_main([
        "--root", str(tmp_path),
        "--db", str(world.database.path),
        "--artifacts", str(world.artifacts),
        "application", "handoff", world.application_id,
    ])
    printed = json.loads(capsys.readouterr().out)

    assert code == 0
    assert printed["handoff"]["package_id"].startswith("hpkg-")
    assert printed["handoff"]["approved_answers_count"] == 2
    assert printed["application"]["state"] == ApplicationState.HANDOFF_IN_PROGRESS.value
    # A visao impressa nao carrega o que o candidato respondeu.
    assert APPROVED_ANSWER not in json.dumps(printed)
    assert CANDIDATE_EMAIL not in json.dumps(printed)
    world.database.close()


def test_preparing_the_handoff_again_reads_the_recorded_package(tmp_path):
    world = _build(tmp_path)
    first = world.service.prepare_handoff(world.application_id)
    second = world.service.prepare_handoff(world.application_id)
    assert second.to_dict() == first.to_dict()
    assert len(world.database.list_handoff_packages(world.application_id)) == 1
    world.database.close()


# --- as invariantes: negativos ------------------------------------------------


def test_a_missing_resume_keeps_the_application_in_needs_human_captcha(tmp_path):
    world = _build(tmp_path)
    world.resume.unlink()
    with pytest.raises(HandoffError, match="resume artifact is missing"):
        world.service.prepare_handoff(world.application_id)
    assert world.database.get_application(world.application_id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    assert world.database.list_handoff_packages(world.application_id) == []
    assert not world.package_dir.exists(), "bundle orfao: o pacote nao deveria ter ficado"
    world.database.close()


def test_a_resume_that_changed_after_approval_blocks_the_handoff(tmp_path):
    world = _build(tmp_path)
    world.resume.write_bytes(b"%PDF-1.4 TAMPERED resume")
    with pytest.raises(HandoffError, match="changed after it was approved"):
        world.service.prepare_handoff(world.application_id)
    assert world.database.get_application(world.application_id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    assert not world.package_dir.exists()
    world.database.close()


def test_answers_that_changed_after_the_review_block_the_handoff(tmp_path):
    world = _build(tmp_path)
    tampered = _form()
    tampered.fields[1].answer.answer = "outra resposta"
    tampered.fields[1].value = "outra resposta"
    world.database.save_application_form(world.application_id, tampered)
    with pytest.raises(HandoffError, match="answers changed after the review snapshot"):
        world.service.prepare_handoff(world.application_id)
    assert world.database.get_application(world.application_id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    world.database.close()


def test_a_resume_hash_that_was_never_recorded_blocks_the_handoff(tmp_path):
    """Sem hash registrado nao ha como provar o que foi aprovado."""
    world = _build(tmp_path)
    application = world.database.get_application(world.application_id)
    application.context.pop("resume_sha256")
    world.database.save_application(application)
    with pytest.raises(HandoffError, match="resume hash recorded"):
        world.service.prepare_handoff(world.application_id)
    world.database.close()


def test_a_rejection_without_recorded_provenance_cannot_be_reconstructed(tmp_path):
    """Sem observacao gravada, reconstruir seria inventar o motivo."""
    world = _build(tmp_path, "legacy", challenge=None)
    with pytest.raises(HandoffError, match="no challenge provenance"):
        world.service.prepare_handoff(world.application_id)
    assert world.database.get_application(world.application_id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    world.database.close()


def test_a_handoff_cannot_start_from_another_state(tmp_path):
    world = _build(tmp_path)
    service = ApplicationService(world.database)
    service.transition(world.application_id, ApplicationState.REVIEW_REACHED, "submit_retry_authorized")
    with pytest.raises(HandoffError, match="requires NEEDS_HUMAN_CAPTCHA"):
        world.service.prepare_handoff(world.application_id)
    world.database.close()


def test_a_destination_with_a_query_is_refused(tmp_path):
    """Query no destino e onde um identificador de sessao viajaria no pacote."""
    world = _build(tmp_path)
    snapshot = world.database.get_review_snapshot(world.application_id)
    snapshot.destination = f"{DESTINATION}?session=abc"
    world.database.save_review_snapshot(snapshot)
    with pytest.raises(HandoffError, match="destination must not carry a query"):
        world.service.prepare_handoff(world.application_id)
    assert world.database.get_application(world.application_id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    world.database.close()


def test_the_observation_cannot_contradict_the_recorded_rejection(tmp_path):
    world = _build(tmp_path)
    with pytest.raises(HandoffError, match="does not match the recorded rejection"):
        world.service.prepare_handoff(
            world.application_id,
            {"provider": "recaptcha", "reason_token": "provider_rejected_submission"},
        )
    assert world.database.get_application(world.application_id).state is ApplicationState.NEEDS_HUMAN_CAPTCHA
    world.database.close()


# --- privacidade e conteudo ---------------------------------------------------


def test_the_event_journal_carries_only_reference_and_short_tokens(tmp_path):
    world = _build(tmp_path)
    package = world.service.prepare_handoff(world.application_id)
    journal = world.database.list_application_events(world.application_id)
    handoff_events = [event for event in journal if event.event == "handoff_started"]

    assert len(handoff_events) == 1
    payload = handoff_events[0].payload
    assert set(payload) == {
        "previous_state",
        "handoff_package_id",
        "reason_token",
        "provider",
        "challenge_session_id",
    }
    serialized = json.dumps([event.payload for event in journal], ensure_ascii=False)
    for secret in (APPROVED_ANSWER, UNAPPROVED_ANSWER, CANDIDATE_EMAIL, "Why do you want this role?"):
        assert secret not in serialized, f"o journal vazou {secret!r}"
    assert payload["handoff_package_id"] == package.package_id
    assert payload["reason_token"] == package.reason_token
    world.database.close()


def test_unapproved_answers_never_enter_the_package(tmp_path):
    world = _build(tmp_path)
    package = world.service.prepare_handoff(world.application_id)
    keys = {row.question_key for row in package.approved_answers}
    assert "why-this-role" in keys
    assert "draft-question" not in keys
    bundle = (world.artifacts / package.package_path).read_text(encoding="utf-8")
    assert UNAPPROVED_ANSWER not in bundle
    assert APPROVED_ANSWER in bundle, "a resposta aprovada e justamente o que o humano precisa"
    world.database.close()


def test_the_digest_covers_the_resume_and_the_approved_answers(tmp_path):
    world = _build(tmp_path)
    package = world.service.prepare_handoff(world.application_id)
    stored = json.loads((world.artifacts / package.package_path).read_text(encoding="utf-8"))

    for field, value in (
        ("resume_sha256", "0" * 64),
        ("answers_fingerprint", "1" * 64),
        ("destination", "https://jobs.lever.co/pavago/other/apply"),
    ):
        tampered = dict(stored)
        tampered[field] = value
        with pytest.raises(HandoffError, match="does not match its digest"):
            HumanHandoffPackage.from_dict(tampered)

    tampered = dict(stored)
    tampered["approved_answers"] = [{"question_key": "why-this-role", "question": "Why?", "answer": "outra"}]
    with pytest.raises(HandoffError, match="does not match its digest"):
        HumanHandoffPackage.from_dict(tampered)
    world.database.close()


# --- imutabilidade ------------------------------------------------------------


def test_a_later_resume_change_does_not_rewrite_the_recorded_package(tmp_path):
    world = _build(tmp_path)
    package = world.service.prepare_handoff(world.application_id)
    recorded = (world.artifacts / package.package_path).read_text(encoding="utf-8")
    copied = (world.artifacts / package.resume_path).read_bytes()

    # O curriculo de origem e o formulario mudam DEPOIS do handoff.
    world.resume.write_bytes(b"%PDF-1.4 regenerated resume")
    world.database.save_application_form(world.application_id, _form())

    after = world.service.load_package(package.package_id)
    assert after.to_dict() == package.to_dict()
    assert (world.artifacts / package.package_path).read_text(encoding="utf-8") == recorded
    assert (world.artifacts / package.resume_path).read_bytes() == copied
    assert after.resume_sha256 == hashlib.sha256(copied).hexdigest()
    world.database.close()


def test_the_package_address_follows_the_recorded_handoff_event(tmp_path):
    """Outra sessao de desafio e OUTRO pacote: o endereco segue a observacao."""
    first = _build(tmp_path, "one")
    second = _build(
        tmp_path,
        "two",
        challenge={
            "provider": "hcaptcha",
            "reason_token": "provider_rejected_submission",
            "session_id": "cs-zzz999",
        },
    )
    one = first.service.prepare_handoff(first.application_id)
    two = second.service.prepare_handoff(second.application_id)
    assert one.challenge_session_id != two.challenge_session_id
    assert one.package_id != two.package_id
    first.database.close()
    second.database.close()
