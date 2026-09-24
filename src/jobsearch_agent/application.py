"""Application domain, state transitions and Safety Gate.

This module has no browser or ATS dependency. It is intentionally usable by a
CLI, Hermes or a future Playwright adapter.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .models import Application, ApplicationAnswer, ApplicationContext, ApplicationEvent, ApplicationField, ApplicationForm, ApplicationPolicy, ApplicationReadiness, ApplicationState, FormCapabilityIssue, SubmissionConfirmationEvidence
from .forms import validate_application_form
from .persistence import ApplicationConflict, Database

if TYPE_CHECKING:  # pragma: no cover - apenas para tipagem, evita ciclo de import
    from .handoff import HumanHandoffPackage


class ApplicationDomainError(ValueError):
    pass


ABANDONED_ATTEMPT_STATUS = "INTERRUPTED"

#: Unicos metadados que o journal de auditoria guarda sobre um handoff. O pacote
#: completo (respostas, curriculo) fica no diretorio privado de artifacts; o
#: evento carrega referencia e tokens curtos, nunca conteudo.
HANDOFF_EVENT_KEYS = ("handoff_package_id", "reason_token", "provider", "challenge_session_id")

_SAFE_EVENT_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _assert_transition(previous: ApplicationState, target: ApplicationState) -> None:
    """Unica regra de aresta valida. Nenhuma porta de escrita pode pular esta."""
    if target not in TRANSITIONS[previous]:
        raise ApplicationDomainError(f"invalid application transition: {previous.value} -> {target.value}")


def _handoff_event_payload(previous: ApplicationState, package: "HumanHandoffPackage | None") -> dict[str, Any]:
    payload: dict[str, Any] = {"previous_state": previous.value}
    if package is None:
        return payload
    payload.update({
        "handoff_package_id": package.package_id,
        "reason_token": package.reason_token,
        "provider": package.provider,
        "challenge_session_id": package.challenge_session_id,
    })
    for key in HANDOFF_EVENT_KEYS:
        value = str(payload[key])
        if value and not _SAFE_EVENT_TOKEN.fullmatch(value):
            # Allowlist fechado: um valor livre aqui seria PII no journal.
            raise ApplicationDomainError(f"handoff event metadata must be an opaque token: {key}")
    return payload


TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.DRAFT: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.PREPARING: {ApplicationState.MATERIALS_READY, ApplicationState.READY_FOR_REVIEW, ApplicationState.READY_TO_APPLY, ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_ARTIFACT, ApplicationState.NEEDS_LOGIN, ApplicationState.NEEDS_MFA, ApplicationState.NEEDS_CAPTCHA, ApplicationState.UNSUPPORTED_FORM, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.MATERIALS_READY: {ApplicationState.READY_FOR_REVIEW, ApplicationState.READY_TO_APPLY, ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_ARTIFACT, ApplicationState.UNSUPPORTED_FORM, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.READY_FOR_REVIEW: {ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REACHED, ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_ARTIFACT, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.READY_TO_APPLY: {ApplicationState.PREPARING, ApplicationState.REVIEW_REACHED, ApplicationState.SUBMIT_AUTHORIZED, ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_ARTIFACT, ApplicationState.UNSUPPORTED_FORM, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.REVIEW_REACHED: {ApplicationState.READY_TO_APPLY, ApplicationState.SUBMIT_AUTHORIZED, ApplicationState.POLICY_BLOCKED},
    ApplicationState.SUBMIT_AUTHORIZED: {ApplicationState.SUBMITTING, ApplicationState.POLICY_BLOCKED, ApplicationState.REVIEW_REACHED},
    # REVIEW_REACHED existe para a tentativa ESTRANDADA: um processo morto no
    # meio da submissao deixa o estado em SUBMITTING sem desfecho registrado, e
    # sem essa aresta a candidatura ficava presa para sempre. A operacao e
    # explicita (retry-submit) e registra que nao havia desfecho.
    ApplicationState.SUBMITTING: {ApplicationState.SUBMITTED, ApplicationState.SUBMIT_FAILED, ApplicationState.SUBMIT_UNKNOWN, ApplicationState.NEEDS_HUMAN_CAPTCHA, ApplicationState.REVIEW_REACHED},
    ApplicationState.SUBMITTED: set(),
    # Nao e terminal: uma falha definitiva pode ser retomada por operacao
    # explicita. SUBMIT_UNKNOWN continua terminal — nao se sabe se foi aceita.
    ApplicationState.SUBMIT_FAILED: {ApplicationState.REVIEW_REACHED},
    ApplicationState.SUBMIT_UNKNOWN: set(),
    ApplicationState.NEEDS_ANSWER: {ApplicationState.PREPARING, ApplicationState.MATERIALS_READY, ApplicationState.READY_FOR_REVIEW, ApplicationState.NEEDS_ARTIFACT, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.NEEDS_ARTIFACT: {ApplicationState.PREPARING, ApplicationState.MATERIALS_READY, ApplicationState.READY_FOR_REVIEW, ApplicationState.NEEDS_ANSWER, ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.NEEDS_LOGIN: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED},
    ApplicationState.NEEDS_MFA: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED},
    ApplicationState.NEEDS_CAPTCHA: {ApplicationState.PREPARING, ApplicationState.POLICY_BLOCKED},
    # Handoff humano. A submissao chegou a sair e o provedor recusou a
    # verificacao anti-bot: o agente nao insiste sozinho. Reabrir exige a
    # operacao explicita `retry-submit`.
    # ADR 0005: retry explicito continua valendo, e o caminho manual e ADITIVO.
    ApplicationState.NEEDS_HUMAN_CAPTCHA: {ApplicationState.REVIEW_REACHED, ApplicationState.HANDOFF_IN_PROGRESS},
    # Cancelar so vale ANTES de qualquer relato de envio.
    ApplicationState.HANDOFF_IN_PROGRESS: {ApplicationState.REVIEW_REACHED, ApplicationState.AWAITING_SUBMISSION_CONFIRMATION},
    # Deliberadamente SEM aresta para REVIEW_REACHED, SUBMIT_AUTHORIZED ou
    # SUBMITTING: existe a possibilidade real de a candidatura ja ter sido
    # enviada, e reabrir o submit violaria exactly-once. So evidencia
    # independente sai daqui.
    ApplicationState.AWAITING_SUBMISSION_CONFIRMATION: {ApplicationState.SUBMITTED},
    ApplicationState.UNSUPPORTED_FORM: {ApplicationState.POLICY_BLOCKED, ApplicationState.REJECTED},
    ApplicationState.POLICY_BLOCKED: {ApplicationState.PREPARING, ApplicationState.REJECTED},
    ApplicationState.REJECTED: set(),
}


def application_id_for_job(job_id: str) -> str:
    return "application-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]


def load_application_policy(path: str | Path) -> ApplicationPolicy:
    source = Path(path)
    if not source.exists():
        return ApplicationPolicy()
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    autonomy = raw.get("autonomy", {}) if isinstance(raw, dict) else {}
    limits = raw.get("limits", {}) if isinstance(raw, dict) else {}
    safety = raw.get("safety", {}) if isinstance(raw, dict) else {}
    providers = raw.get("providers", {}) if isinstance(raw, dict) else {}
    policy = ApplicationPolicy()
    policy.autonomy.update({str(key): str(value) for key, value in autonomy.items()})
    policy.providers = {
        str(provider): {str(key): str(value) for key, value in values.items()}
        for provider, values in providers.items()
        if isinstance(values, dict)
    }
    policy.applications_per_day = int(limits.get("applications_per_day", policy.applications_per_day))
    policy.unknown_answer = str(safety.get("unknown_answer", policy.unknown_answer))
    policy.captcha = str(safety.get("captcha", policy.captcha))
    policy.mfa = str(safety.get("mfa", policy.mfa))
    policy.legal_question = str(safety.get("legal_question", policy.legal_question))
    return policy


def context_from_dict(data: dict[str, Any]) -> ApplicationContext:
    policy_data = data.get("policy", {}) or {}
    policy = ApplicationPolicy(**{key: value for key, value in policy_data.items() if key in {"autonomy", "providers", "applications_per_day", "unknown_answer", "captcha", "mfa", "legal_question"}})
    form_data = data.get("form")
    form = None
    if isinstance(form_data, dict):
        fields = []
        for raw_field in form_data.get("fields", []):
            if not isinstance(raw_field, dict):
                continue
            answer_data = raw_field.get("answer")
            answer = ApplicationAnswer(**answer_data) if isinstance(answer_data, dict) else None
            fields.append(ApplicationField(**{key: value for key, value in raw_field.items() if key in {"key", "label", "field_type", "semantic_type", "required", "options", "value", "confidence", "source", "step", "attachment_path", "accepted_types", "multiple", "disabled", "semantic_context"}}, answer=answer))
        issues = [FormCapabilityIssue(**item) for item in form_data.get("capability_issues", []) if isinstance(item, dict)]
        form = ApplicationForm(
            form_id=str(form_data.get("form_id", "")),
            provider=str(form_data.get("provider", "generic")),
            fields=fields,
            source=str(form_data.get("source", "fixture")),
            steps=[str(item) for item in form_data.get("steps", [])],
            artifact_root=str(form_data.get("artifact_root", "")),
            capability_issues=issues,
            # Sem estes dois, o destino declarado pelo formulario se perdia ao
            # recarregar a Application (retry, resume, processo reiniciado).
            action=str(form_data.get("action", "")),
            method=str(form_data.get("method", "POST")),
        )
    answers = [ApplicationAnswer(**item) for item in data.get("answers", []) if isinstance(item, dict)]
    return ApplicationContext(
        application_id=str(data.get("application_id", "")),
        job_id=str(data.get("job_id", "")),
        fit=data.get("fit", {}) or {},
        resume=data.get("resume", {}) or {},
        validation=data.get("validation", {}) or {},
        answers=answers,
        form=form,
        policy=policy,
    )


class ApplicationService:
    def __init__(self, database: Database):
        self.database = database

    def create_for_job(self, job_id: str) -> Application:
        existing = self.database.get_application_for_job(job_id)
        if existing:
            return existing
        if not self.database.get_job(job_id):
            raise ApplicationDomainError(f"job not found: {job_id}")
        application = Application(application_id_for_job(job_id), job_id)
        self.database.save_application(application)
        return application

    def transition(
        self,
        application_id: str,
        target: ApplicationState,
        event: str,
        payload: dict[str, Any] | None = None,
        expected_state: ApplicationState | None = None,
        confirmation_evidence: SubmissionConfirmationEvidence | None = None,
    ) -> Application:
        """Unica porta de mudanca de estado.

        Invariante do ADR 0005: nenhum comando baseado SO em declaracao do
        usuario leva a `SUBMITTED`. Exigir a evidencia AQUI, no unico lugar que
        muda estado, torna isso mecanico — um comando novo que alguem venha a
        expor no CLI nao consegue contornar por esquecimento.
        """
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        previous_state = expected_state or application.state
        _assert_transition(previous_state, target)
        if target is ApplicationState.SUBMITTED and confirmation_evidence is None:
            raise ApplicationDomainError(
                "SUBMITTED requires independent confirmation evidence; "
                "a user declaration alone can never satisfy it"
            )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        application.state = target
        application.updated_at = now
        self.database.save_application_transition(application, ApplicationEvent(application.id, previous_state, target, event, payload or {}, now))
        return application

    def retry_submit(self, application_id: str) -> Application:
        """Reabre uma Application cuja submissao falhou de forma definitiva.

        Vale para SUBMIT_FAILED e tambem para uma tentativa **estrandada** em
        SUBMITTING (processo interrompido no meio, sem desfecho registrado): sem
        isso a candidatura ficava presa para sempre, porque SUBMITTING so
        transita para um dos tres desfechos. Um desfecho definitivo desconhecido
        (SUBMIT_UNKNOWN) nunca volta, porque nao se sabe se a candidatura foi
        aceita e reenviar poderia duplicar. A operacao e explicita e registrada,
        e o evento diz se havia desfecho registrado.
        """
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        reopenable = {
            ApplicationState.SUBMIT_FAILED,
            ApplicationState.SUBMITTING,
            ApplicationState.SUBMIT_AUTHORIZED,
            ApplicationState.NEEDS_HUMAN_CAPTCHA,
        }
        if application.state not in reopenable and application.state != ApplicationState.REVIEW_REACHED:
            raise ApplicationDomainError(
                f"submit retry requires SUBMIT_FAILED, SUBMITTING or SUBMIT_AUTHORIZED, got {application.state.value}"
            )
        attempts = self.database.list_submission_attempts(application_id)
        if not attempts:
            raise ApplicationDomainError("submit retry requires a recorded attempt")
        last = attempts[-1]
        stranded = last.status == ApplicationState.SUBMITTING.value
        if application.state == ApplicationState.REVIEW_REACHED:
            if not stranded:
                # Ja reaberta numa chamada anterior e sem tentativa presa: nada
                # a fazer. Repetir a operacao e seguro.
                return application
            # Reparo idempotente: o estado ja foi reaberto numa chamada anterior
            # e so a tentativa estranda ficou para tras, bloqueando o guard de
            # duplicidade. Fechar a tentativa e o unico efeito que falta.
            self.database.abandon_submission_attempt(last.id, last.intent_id)
            return application
        allowed = {
            ApplicationState.SUBMIT_FAILED: {ApplicationState.SUBMIT_FAILED.value},
            ApplicationState.SUBMITTING: {ApplicationState.SUBMITTING.value},
            # Autorizada mas nunca iniciada: a tentativa anterior e que ficou
            # presa. INTERRUPTED ja foi adjudicada por uma operacao explicita
            # anterior, entao nao bloqueia uma nova tentativa.
            ApplicationState.SUBMIT_AUTHORIZED: {
                ApplicationState.SUBMITTING.value,
                ApplicationState.SUBMIT_FAILED.value,
                ABANDONED_ATTEMPT_STATUS,
            },
            # O provedor ja recusou de forma clara uma submissao entregue: nao ha
            # ambiguidade sobre duplicidade, entao o operador pode reabrir.
            ApplicationState.NEEDS_HUMAN_CAPTCHA: {
                "NEEDS_HUMAN_CAPTCHA",
                ApplicationState.SUBMIT_FAILED.value,
            },
        }[application.state]
        if last.status not in allowed:
            raise ApplicationDomainError(
                f"submit retry refused: last attempt is {last.status}; "
                "a submission with unknown outcome must never be resent"
            )
        if stranded:
            # Sem isso a tentativa estrandada continuava em SUBMITTING e o guard
            # de duplicidade recusava toda tentativa seguinte.
            self.database.abandon_submission_attempt(last.id, last.intent_id)
        return self.transition(
            application_id,
            ApplicationState.REVIEW_REACHED,
            "submit_retry_authorized",
            {
                "previous_state": application.state.value,
                "attempt_id": last.id,
                "attempt_status": last.status,
                "attempt_outcome_recorded": last.status != ApplicationState.SUBMITTING.value,
            },
        )

    def start_human_handoff(
        self,
        application_id: str,
        *,
        package: "HumanHandoffPackage | None" = None,
    ) -> Application:
        """command START_HUMAN_HANDOFF: NEEDS_HUMAN_CAPTCHA -> HANDOFF_IN_PROGRESS.

        Com ``package``, o pacote e o evento entram na MESMA transacao: ou o
        humano recebe o que precisa para terminar, ou a Application continua em
        ``NEEDS_HUMAN_CAPTCHA``. Sem pacote continua existindo o handoff generico
        (sem material), que nao persiste bundle.
        """
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        if application.state is not ApplicationState.NEEDS_HUMAN_CAPTCHA:
            raise ApplicationDomainError(
                f"handoff requires NEEDS_HUMAN_CAPTCHA, got {application.state.value}"
            )
        payload = _handoff_event_payload(application.state, package)
        if package is None:
            return self.transition(
                application_id,
                ApplicationState.HANDOFF_IN_PROGRESS,
                "handoff_started",
                payload,
            )
        _assert_transition(application.state, ApplicationState.HANDOFF_IN_PROGRESS)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        application.state = ApplicationState.HANDOFF_IN_PROGRESS
        application.updated_at = now
        event = ApplicationEvent(
            application.id,
            ApplicationState.NEEDS_HUMAN_CAPTCHA,
            ApplicationState.HANDOFF_IN_PROGRESS,
            "handoff_started",
            payload,
            now,
        )
        try:
            self.database.save_handoff_package(package, application, event)
        except ApplicationConflict as exc:
            raise ApplicationDomainError(str(exc)) from exc
        return application

    def cancel_handoff(self, application_id: str) -> Application:
        """command CANCEL_HANDOFF: HANDOFF_IN_PROGRESS -> REVIEW_REACHED.

        Seguro porque ainda NAO houve declaracao de envio. Depois do relato esta
        operacao nao existe: reconciliar exige fluxo explicito.
        """
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        if application.state is not ApplicationState.HANDOFF_IN_PROGRESS:
            raise ApplicationDomainError(
                f"cancel handoff requires HANDOFF_IN_PROGRESS, got {application.state.value}"
            )
        return self.transition(
            application_id,
            ApplicationState.REVIEW_REACHED,
            "handoff_cancelled",
            {"previous_state": application.state.value},
        )

    def report_manual_submission(self, application_id: str) -> Application:
        """event MANUAL_SUBMISSION_REPORTED: HANDOFF_IN_PROGRESS -> AWAITING_...

        NAO marca SUBMITTED. O relato e o que cria a necessidade de confirmacao;
        nao pode ser tambem o que a satisfaz.
        """
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        if application.state is not ApplicationState.HANDOFF_IN_PROGRESS:
            raise ApplicationDomainError(
                f"manual submission report requires HANDOFF_IN_PROGRESS, got {application.state.value}"
            )
        return self.transition(
            application_id,
            ApplicationState.AWAITING_SUBMISSION_CONFIRMATION,
            "manual_submission_reported",
            {"previous_state": application.state.value},
        )

    def confirm_submission(
        self, application_id: str, evidence: SubmissionConfirmationEvidence
    ) -> Application:
        """event SUBMISSION_CONFIRMED: AWAITING_... -> SUBMITTED, SO com evidencia."""
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        if application.state is not ApplicationState.AWAITING_SUBMISSION_CONFIRMATION:
            raise ApplicationDomainError(
                f"confirmation requires AWAITING_SUBMISSION_CONFIRMATION, got {application.state.value}"
            )
        return self.transition(
            application_id,
            ApplicationState.SUBMITTED,
            "submission_confirmed",
            {
                "previous_state": application.state.value,
                "evidence_source": evidence.source.value,
                "evidence_reference": evidence.reference,
            },
            confirmation_evidence=evidence,
        )

    def resume(self, application_id: str) -> Application:
        application = self.database.get_application(application_id)
        if not application:
            raise ApplicationDomainError(f"application not found: {application_id}")
        resumable = {ApplicationState.NEEDS_ANSWER, ApplicationState.NEEDS_ARTIFACT, ApplicationState.NEEDS_LOGIN, ApplicationState.NEEDS_MFA, ApplicationState.NEEDS_CAPTCHA}
        if application.state not in resumable:
            raise ApplicationDomainError(f"application state cannot be resumed: {application.state.value}")
        return self.transition(application_id, ApplicationState.PREPARING, "application_resumed", {"previous_state": application.state.value})


def evaluate_safety_gate(context: ApplicationContext) -> ApplicationReadiness:
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    all_fit_blockers = list(context.fit.get("blockers", []))
    unknown_fit = [item for item in all_fit_blockers if item == "work_authorization_unknown"]
    fit_blockers = [item for item in all_fit_blockers if item not in unknown_fit]
    checks.append({"gate": "fit", "result": "blocker" if fit_blockers else "pass", "evidence": fit_blockers or context.fit.get("criteria", [])})
    if fit_blockers:
        blockers.extend(f"fit:{item}" for item in fit_blockers)
    if unknown_fit:
        checks.append({"gate": "work_authorization", "result": "unknown", "evidence": unknown_fit})
        blockers.append("unknown_answer:work_authorization")

    validation = context.validation
    resume_ok = bool(validation.get("valid")) and bool(validation.get("facts", {}).get("valid", True)) and bool(validation.get("ats", {}).get("valid", True))
    checks.append({"gate": "resume_grounding", "result": "pass" if resume_ok else "blocker", "evidence": validation})
    if not resume_ok:
        blockers.append("resume_invalid")

    form_analyzed = context.form is not None
    capability_issues = list(context.form.capability_issues) if context.form else []
    capability_blockers = [issue for issue in capability_issues if issue.severity == "blocker"]
    if capability_blockers:
        checks.append({"gate": "capabilities", "result": "blocker", "evidence": capability_blockers})
        blockers.append("unsupported_form")
    elif capability_issues:
        checks.append({"gate": "capabilities", "result": "warning", "evidence": capability_issues})
    form_validation = validate_application_form(context.form) if context.form else None
    unsupported_fields = list((form_validation.details if form_validation else {}).get("unsupported_fields", []))
    field_results = (form_validation.details if form_validation else {}).get("field_results", {})
    missing_fields = [key for key, result in field_results.items() if result.get("code") == "MISSING_VALUE"]
    option_fields = [key for key, result in field_results.items() if result.get("code") == "INVALID_OPTION"]
    artifact_fields = [key for key, result in field_results.items() if result.get("code") in {"MISSING_ARTIFACT", "INVALID_ARTIFACT"}]
    invalid_fields = [key for key, result in field_results.items() if not result.get("valid") and key not in unsupported_fields and key not in missing_fields and key not in option_fields and key not in artifact_fields]
    unknown_fields = missing_fields + option_fields
    if form_validation and not form_validation.valid:
        checks.append({"gate": "form_validation", "result": "blocker", "evidence": form_validation.details, "errors": form_validation.errors})
    if unsupported_fields:
        checks.append({"gate": "form", "result": "blocker", "evidence": unsupported_fields})
        blockers.append("unsupported_form")
    else:
        if missing_fields:
            blockers.extend(f"unknown_answer:{item}" for item in missing_fields)
        if option_fields:
            blockers.extend(f"invalid_option:{item}" for item in option_fields)
        if artifact_fields:
            blockers.extend(f"invalid_artifact:{item}" for item in artifact_fields)
            checks.append({"gate": "artifacts", "result": "blocker", "evidence": artifact_fields})
        if invalid_fields:
            blockers.extend(f"invalid_field:{item}" for item in invalid_fields)
            checks.append({"gate": "form_fields", "result": "blocker", "evidence": invalid_fields})
        if unknown_fields:
            checks.append({"gate": "required_answers", "result": "unknown", "evidence": unknown_fields})
        elif not artifact_fields and not invalid_fields and form_analyzed:
            checks.append({"gate": "required_answers", "result": "pass", "evidence": []})

    if not form_analyzed:
        checks.append({"gate": "form", "result": "not_analyzed", "evidence": "No ApplicationForm has been inspected yet."})
    provider_name = context.form.provider if context.form else ""
    provider_policy = context.policy.providers.get(provider_name, {})
    fill_forms_mode = provider_policy.get("fill_forms", context.policy.autonomy.get("fill_forms", "review"))
    submit_mode = provider_policy.get("submit", context.policy.autonomy.get("submit", "manual"))
    if provider_name and "advance_steps" in provider_policy:
        advance_steps_mode = provider_policy["advance_steps"]
    else:
        advance_steps_mode = "review"
    if "unsupported_form" in blockers:
        decision = ApplicationState.UNSUPPORTED_FORM
    elif artifact_fields:
        decision = ApplicationState.NEEDS_ARTIFACT
    elif unknown_fields or unknown_fit:
        decision = ApplicationState.NEEDS_ANSWER
    elif invalid_fields:
        decision = ApplicationState.NEEDS_ANSWER
    elif not form_analyzed:
        decision = ApplicationState.READY_FOR_REVIEW
    elif fill_forms_mode != "auto":
        decision = ApplicationState.READY_FOR_REVIEW
    elif fit_blockers or not resume_ok:
        decision = ApplicationState.REJECTED
    else:
        decision = ApplicationState.READY_TO_APPLY
    if fit_blockers or not resume_ok:
        decision = ApplicationState.REJECTED
    requires_review = decision == ApplicationState.READY_FOR_REVIEW or submit_mode != "auto"
    checks.append({"gate": "authorization", "result": "review" if requires_review else "authorized", "evidence": {"provider": provider_name, "fill_forms": fill_forms_mode, "advance_steps": advance_steps_mode, "submit": submit_mode}})
    return ApplicationReadiness(decision, decision == ApplicationState.READY_TO_APPLY, requires_review, checks, blockers)
