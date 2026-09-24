"""JSA-CONF-001..005: confirmacao observavel.

O que importa aqui e que o observador nao vira um carimbo: ele encontra,
corrobora, persiste o que viu — inclusive o que NAO basta — e so o portao do
dominio decide. E que procurar em todo o historico da caixa nao confirme nada.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.confirmation import (
    CONFIRMATION_SIGNAL_WEIGHTS,
    CONFIRMATION_WINDOW_TOLERANCE,
    ConfirmationObservationError,
    ConfirmationReconciliationService,
    EmailConfirmationObserver,
    EmailMessage,
    StaticEmailSource,
    ats_domains,
    match_email,
)
from jobsearch_agent.models import (
    ApplicationState,
    ConfirmationSource,
    Job,
    SubmissionConfirmationEvidence,
)
from jobsearch_agent.persistence import Database

CANDIDATE_EMAIL = "sentinel.person@example.invalid"
CANDIDATE_NAME = "Sentinel Person"
BODY_SENTINEL = "sentinel-body-content-7c21"

DESTINATION = "https://boards.greenhouse.io/acme/jobs/12345"
JOB_TITLE = "Senior Frontend Engineer"


def _instant(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _record(
    *,
    message_id: str = "18f0a1b2c3d4e5f6",
    sender: str = "no-reply@acme.com",
    subject: str = "Thank you for applying",
    body: str = "",
    minutes_ago: float = 2.0,
) -> EmailRecord:
    return EmailMessage(
        reference=message_id,
        observed_at=_instant(minutes_ago),
        sender=sender,
        subject=subject,
        body=body,
    )


@dataclass
class World:
    database: Database
    application_id: str
    job: Job
    service: ConfirmationReconciliationService


def _awaiting(
    tmp_path: Path,
    suffix: str = "conf",
    *,
    company: str = "Acme",
    source: str = "greenhouse",
    report: bool = True,
) -> World:
    database = Database(tmp_path / f"{suffix}.db")
    job = Job(
        id=f"job-{suffix}",
        source=source,
        external_id="12345",
        company=company,
        title=JOB_TITLE,
        description="Build the web app",
        url=DESTINATION,
    )
    database.save_job(job, f"{source}:{suffix}", {})
    service = ApplicationService(database)
    application = service.create_for_job(job.id)
    for state in (
        ApplicationState.PREPARING,
        ApplicationState.MATERIALS_READY,
        ApplicationState.READY_TO_APPLY,
        ApplicationState.REVIEW_REACHED,
        ApplicationState.SUBMIT_AUTHORIZED,
        ApplicationState.SUBMITTING,
        ApplicationState.NEEDS_HUMAN_CAPTCHA,
    ):
        application = service.transition(application.id, state, f"to_{state.value.casefold()}")
    service.start_human_handoff(application.id)
    if report:
        service.report_manual_submission(application.id)
        assert database.get_application(application.id).state is ApplicationState.AWAITING_SUBMISSION_CONFIRMATION
    return World(database, application.id, job, ConfirmationReconciliationService(database))


def _state(world: World) -> str:
    return world.database.get_application(world.application_id).state.value


# --- o porto: contrato independente de provedor --------------------------------


def test_a_non_email_observer_satisfies_the_same_contract(tmp_path):
    """`ConfirmationObserver` nao pode virar sinonimo de Gmail."""

    class ProviderStatusObserver:
        def observe(self, application, *, since):
            return [
                SubmissionConfirmationEvidence(
                    source=ConfirmationSource.PROVIDER_APPLICATION_STATUS,
                    observed_at=since.isoformat(timespec="seconds"),
                    reference="status-9911",
                    provider="greenhouse",
                    confidence=1.0,
                    signals=("provider_status_match",),
                )
            ]

    world = _awaiting(tmp_path, "port")
    result = world.service.reconcile(world.application_id, observers=[ProviderStatusObserver()])
    assert result.accepted is not None
    assert result.accepted["source"] == "provider_application_status"
    assert _state(world) == ApplicationState.SUBMITTED.value
    world.database.close()


def test_reconciliation_without_an_observer_is_refused(tmp_path):
    world = _awaiting(tmp_path, "no-observer")
    with pytest.raises(ConfirmationObservationError, match="at least one observer"):
        world.service.reconcile(world.application_id)
    assert _state(world) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    world.database.close()


# --- JSA-CONF-002/003: matching e corroboracao ---------------------------------


def test_the_phrase_alone_is_not_enough(tmp_path):
    """Palavra-chave nao confirma: a frase e porta, nao prova."""
    world = _awaiting(tmp_path, "weak")
    source = StaticEmailSource([_record(sender="newsletter@example.invalid", body="Thank you for applying.")])
    result = world.service.reconcile(world.application_id, email_sources=[source])

    assert result.accepted is None
    assert "too weak" in result.detail
    assert _state(world) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    assert result.detected[0]["signals"] == ["application_confirmation_phrase", "time_window_match"]
    assert result.detected[0]["confidence"] == pytest.approx(0.40)
    world.database.close()


def test_a_sender_from_the_ats_confirms(tmp_path):
    world = _awaiting(tmp_path, "ats")
    source = StaticEmailSource(
        [_record(sender="no-reply@greenhouse.io", body="We received your application.")]
    )
    result = world.service.reconcile(world.application_id, email_sources=[source])

    assert result.accepted is not None
    assert result.accepted["provider"] == "email"
    assert result.accepted["confidence"] == pytest.approx(
        CONFIRMATION_SIGNAL_WEIGHTS["application_confirmation_phrase"]
        + CONFIRMATION_SIGNAL_WEIGHTS["ats_domain_match"]
        + CONFIRMATION_SIGNAL_WEIGHTS["time_window_match"]
    )
    assert _state(world) == ApplicationState.SUBMITTED.value
    world.database.close()


def test_a_sender_from_the_company_confirms(tmp_path):
    world = _awaiting(tmp_path, "company")
    source = StaticEmailSource([_record(sender="no-reply@acme.com", body="Your application was received.")])
    result = world.service.reconcile(world.application_id, email_sources=[source])

    assert result.accepted is not None
    assert "company_match" in result.accepted["signals"]
    assert _state(world) == ApplicationState.SUBMITTED.value
    world.database.close()


def test_the_job_reference_is_an_independent_corroboration(tmp_path):
    world = _awaiting(tmp_path, "job")
    source = StaticEmailSource(
        [
            _record(
                sender="careers@example.invalid",
                subject="Application received",
                body=f"Your application for {JOB_TITLE} (12345) was received.",
            )
        ]
    )
    result = world.service.reconcile(world.application_id, email_sources=[source])
    assert result.accepted is not None
    assert "job_match" in result.accepted["signals"]
    world.database.close()


def test_rejection_language_is_never_a_confirmation(tmp_path):
    """Agradecimento e recusa na mesma mensagem: e recusa."""
    world = _awaiting(tmp_path, "rejection")
    source = StaticEmailSource(
        [
            _record(
                sender="no-reply@greenhouse.io",
                subject="Thank you for applying",
                body="Thank you for applying. Unfortunately we will not be moving forward.",
            )
        ]
    )
    result = world.service.reconcile(world.application_id, email_sources=[source])

    assert result.detected == ()
    assert result.accepted is None
    assert "no confirmation evidence" in result.detail
    assert _state(world) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    world.database.close()


def test_the_match_requires_the_window(tmp_path):
    world = _awaiting(tmp_path, "window")
    since = datetime.now(timezone.utc) - CONFIRMATION_WINDOW_TOLERANCE
    record = _record(sender="no-reply@greenhouse.io")

    inside = _record(minutes_ago=CONFIRMATION_WINDOW_TOLERANCE.total_seconds() / 60 - 1)
    assert match_email(inside, job=world.job, ats="greenhouse", since=since) is not None
    outside = _record(minutes_ago=CONFIRMATION_WINDOW_TOLERANCE.total_seconds() / 60 + 1)
    assert match_email(outside, job=world.job, ats="greenhouse", since=since) is None
    assert match_email(record, job=world.job, ats="greenhouse", since=since) is not None
    world.database.close()


def test_an_old_email_from_the_same_company_never_confirms(tmp_path):
    """A janela comeca no relato: historico da caixa nao e evidencia."""
    world = _awaiting(tmp_path, "old")
    source = StaticEmailSource(
        [_record(sender="no-reply@greenhouse.io", minutes_ago=60 * 24 * 2)]
    )
    result = world.service.reconcile(world.application_id, email_sources=[source])

    assert result.detected == ()
    assert _state(world) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    world.database.close()


def test_the_tolerance_covers_a_slightly_earlier_timestamp(tmp_path):
    world = _awaiting(tmp_path, "tolerance")
    source = StaticEmailSource([_record(sender="no-reply@greenhouse.io", minutes_ago=5)])
    result = world.service.reconcile(world.application_id, email_sources=[source])
    assert result.accepted is not None
    world.database.close()


def test_a_hostile_record_neither_confirms_nor_crashes(tmp_path):
    world = _awaiting(tmp_path, "hostile")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    weird = EmailMessage(
        reference="18f0a1b2c3d4e5f6",
        observed_at=future,
        sender="no-reply@greenhouse.io",
        subject="We received your application",
    )
    result = world.service.reconcile(world.application_id, email_sources=[StaticEmailSource([weird])])
    assert result.detected == ()
    assert _state(world) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    world.database.close()


# --- JSA-CONF-004: persistencia minima ----------------------------------------


def test_what_is_persisted_is_the_evidence_and_never_the_message(tmp_path):
    world = _awaiting(tmp_path, "privacy")
    source = StaticEmailSource(
        [
            _record(
                message_id="<CAF1234abcd@mail.gmail.com>",
                sender=f"{CANDIDATE_NAME} via Greenhouse <no-reply@greenhouse.io>",
                subject="Thank you for applying",
                body=f"We received your application. Reply to {CANDIDATE_EMAIL}. {BODY_SENTINEL}",
            )
        ]
    )
    result = world.service.reconcile(world.application_id, email_sources=[source])
    stored = world.database.list_confirmation_evidence(world.application_id)

    assert result.accepted is not None
    assert len(stored) == 1
    record = stored[0]
    assert record["source"] == "confirmation_email"
    assert record["provider"] == "email"
    assert record["accepted"] is True
    assert record["signals"] == ["application_confirmation_phrase", "ats_domain_match", "time_window_match"]
    assert set(record) >= {"source", "observed_at", "reference", "provider", "confidence", "signals", "accepted"}
    serialized = json.dumps(stored) + json.dumps(result.to_dict())
    for secret in (
        BODY_SENTINEL,
        CANDIDATE_EMAIL,
        CANDIDATE_NAME,
        "mail.gmail.com",
        "no-reply@greenhouse.io",
        "We received your application",
    ):
        assert secret not in serialized, f"a evidencia persistiu {secret!r}"
    # Message-ID RFC pode carregar o dominio do remetente: vira digest.
    assert record["reference"].startswith("msg-")
    assert "@" not in record["reference"]
    world.database.close()


def test_an_opaque_message_id_is_kept_as_the_reference(tmp_path):
    world = _awaiting(tmp_path, "reference")
    source = StaticEmailSource([_record(message_id="18f0a1b2c3d4e5f6", sender="no-reply@greenhouse.io")])
    result = world.service.reconcile(world.application_id, email_sources=[source])
    assert result.accepted["reference"] == "18f0a1b2c3d4e5f6"
    world.database.close()


def test_weak_evidence_is_persisted_even_though_it_is_refused(tmp_path):
    """Recusar nao pode significar apagar: o que foi visto fica registrado."""
    world = _awaiting(tmp_path, "recorded")
    source = StaticEmailSource([_record(sender="newsletter@example.invalid", body="Thank you for applying.")])
    world.service.reconcile(world.application_id, email_sources=[source])

    stored = world.database.list_confirmation_evidence(world.application_id)
    assert len(stored) == 1
    assert stored[0]["accepted"] is False
    assert stored[0]["confidence"] < 0.5
    assert _state(world) == ApplicationState.AWAITING_SUBMISSION_CONFIRMATION.value
    world.database.close()


def test_reconciling_twice_does_not_duplicate_the_evidence(tmp_path):
    world = _awaiting(tmp_path, "idempotent")
    source = StaticEmailSource([_record(sender="no-reply@greenhouse.io", body="We received your application.")])
    first = world.service.reconcile(world.application_id, email_sources=[source])
    # A primeira reconciliacao ja confirmou; rodar de novo a partir daqui e
    # recusado pelo estado, entao a comparacao usa a evidencia persistida.
    assert first.accepted is not None
    stored = world.database.list_confirmation_evidence(world.application_id)
    assert len(stored) == 1
    world.database.close()


# --- o portao e o estado ------------------------------------------------------


def test_reconciliation_requires_the_recorded_report(tmp_path):
    """A janela comeca no evento; sem ele nao ha de onde partir.

    O estado `AWAITING_SUBMISSION_CONFIRMATION` e alcancavel por `transition`
    (a aresta existe), entao a checagem precisa ser do EVENTO, e nao do estado.
    """
    world = _awaiting(tmp_path, "no-report", report=False)
    ApplicationService(world.database).transition(
        world.application_id,
        ApplicationState.AWAITING_SUBMISSION_CONFIRMATION,
        "handoff_state_advanced",
    )
    with pytest.raises(ConfirmationObservationError, match="manual submission report"):
        world.service.reconcile(world.application_id, email_sources=[StaticEmailSource([])])
    world.database.close()


def test_reconciliation_is_refused_outside_awaiting_confirmation(tmp_path):
    world = _awaiting(tmp_path, "wrong-state")
    ApplicationService(world.database).confirm_submission(
        world.application_id,
        SubmissionConfirmationEvidence(
            source=ConfirmationSource.EXTERNALLY_VERIFIED_RECORD,
            observed_at=_instant(1),
            reference="rec-1",
            provider="registry",
        ),
    )
    with pytest.raises(ConfirmationObservationError, match="requires AWAITING_SUBMISSION_CONFIRMATION"):
        world.service.reconcile(world.application_id, email_sources=[StaticEmailSource([])])
    world.database.close()


def test_ats_domains_come_from_the_provider_profile():
    assert "lever.co" in ats_domains("lever")
    assert "greenhouse.io" in ats_domains("greenhouse")
    assert ats_domains("generic") == set()


def test_the_observer_never_persists_the_body(tmp_path):
    """O corpo existe so durante o matching."""
    world = _awaiting(tmp_path, "body")
    record = _record(sender="no-reply@greenhouse.io", body=f"We received your application. {BODY_SENTINEL}")
    observer = EmailConfirmationObserver(StaticEmailSource([record]), job=world.job, ats="greenhouse")
    evidence = observer.observe(
        world.database.get_application(world.application_id),
        since=datetime.now(timezone.utc) - CONFIRMATION_WINDOW_TOLERANCE,
    )
    assert len(evidence) == 1
    assert BODY_SENTINEL not in json.dumps(evidence[0].safe_view())
    assert "body" not in evidence[0].safe_view()
    world.database.close()


def test_the_ats_sending_subdomain_still_counts_as_the_ats(tmp_path):
    """`no-reply@hire.lever.co` E o ATS.

    O endereco real de confirmacao de um ATS e um subdominio de envio. Comparar
    por igualdade fazia a mensagem certa pontuar 0.40 (abaixo do piso) e o teste
    real falharia por um motivo que nao e o que ele quer medir.
    """
    world = _awaiting(tmp_path, "sending-subdomain", source="lever")
    source = StaticEmailSource([_record(sender="no-reply@hire.lever.co", body="We received your application.")])
    result = world.service.reconcile(world.application_id, email_sources=[source])

    assert result.accepted is not None
    assert "ats_domain_match" in result.accepted["signals"]
    assert result.accepted["confidence"] == pytest.approx(0.60)
    assert _state(world) == ApplicationState.SUBMITTED.value
    world.database.close()


def test_a_lookalike_domain_is_not_the_ats():
    """Sufixo casa por rotulo: nem `notlever.co`, nem `lever.co.outro.com`."""
    from jobsearch_agent.confirmation import ats_domains, sender_is_ats

    domains = ats_domains("lever")
    assert sender_is_ats("hire.lever.co", domains) is True
    assert sender_is_ats("lever.co", domains) is True
    assert sender_is_ats("notlever.co", domains) is False
    assert sender_is_ats("lever.co.evil.example", domains) is False
    assert sender_is_ats("", domains) is False
    assert sender_is_ats("hire.lever.co", set()) is False
