"""CLI JSON-first para execução local e consumo pelo Hermes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .application import ApplicationDomainError, ApplicationService, context_from_dict
from .config import Settings
from .confirmation import ConfirmationObservationError, ConfirmationSourceUnavailable
from .greenhouse import GreenhouseSubmissionExecutor
from .handoff import HumanHandoffService
from .integrations.email.oauth import GMAIL_READONLY_SCOPE, GmailAuthError, GmailConfigError
from .llm import LLMError
from .linkedin.inspector import LinkedInInspector
from .models import to_dict
from .persistence import ApplicationConflict, Database
from .pipeline import PipelineError, analyze, apply_live, discover, dry_run_application, ingest, ingest_url, precheck_job, prepare, prepare_application, resume_application, run, search
from .preflight import run_preflight
from .profile import ProfileError, load_facts, load_preferences, load_profile, validate_facts, validate_profile_readiness
from .submission import (
    LiveNetworkPolicy,
    SubmissionBoundaryError,
    SubmissionService,
    build_review_snapshot,
    build_submission_payload,
    review_field_rows,
)
from .sources import SourceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobsearch-agent", description="Analisa vagas e prepara currículos grounded.")
    parser.add_argument("--version", action="version", version="jobsearch-agent 0.1.0")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="raiz do projeto")
    parser.add_argument("--db", default="data/jobsearch.db")
    parser.add_argument("--artifacts", default="data/applications")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--facts", default=None)
    parser.add_argument("--real-profile", action="store_true", help="recusa fixtures demo")
    parser.add_argument("--preferences", default=None)
    parser.add_argument("--answers", default=None)
    parser.add_argument("--application-policy", default="profile/application_policy.yaml")
    parser.add_argument("--language", choices=["pt-BR", "en-US"], default=None)
    sub = parser.add_subparsers(dest="command")

    def runtime_options(command: argparse.ArgumentParser) -> None:
        # Aceita opções tanto antes quanto depois do subcomando, como nos exemplos
        # do README e nos prompts do Hermes.
        command.add_argument("--db", default=argparse.SUPPRESS)
        command.add_argument("--artifacts", default=argparse.SUPPRESS)
        command.add_argument("--profile", default=argparse.SUPPRESS)
        command.add_argument("--facts", default=argparse.SUPPRESS)
        command.add_argument("--preferences", default=argparse.SUPPRESS)
        command.add_argument("--answers", default=argparse.SUPPRESS)
        command.add_argument("--application-policy", default=argparse.SUPPRESS)
        command.add_argument("--real-profile", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("--language", choices=["pt-BR", "en-US"], default=argparse.SUPPRESS)

    profile = sub.add_parser("profile", help="valida o Career Profile")
    profile_sub = profile.add_subparsers(dest="profile_command")
    profile_validate = profile_sub.add_parser("validate")
    runtime_options(profile_validate)
    profile_validate.set_defaults(handler="profile_validate")
    profile_readiness = profile_sub.add_parser("readiness", help="avalia completude para dry-run real")
    runtime_options(profile_readiness)
    profile_readiness.set_defaults(handler="profile_readiness")

    ingest_parser = sub.add_parser("ingest", help="ingere uma vaga")
    runtime_options(ingest_parser)
    ingest_group = ingest_parser.add_mutually_exclusive_group(required=True)
    ingest_group.add_argument("--url")
    ingest_group.add_argument("--json-file", type=Path)
    ingest_parser.set_defaults(handler="ingest")

    search_parser = sub.add_parser("search", help="descobre vagas via JobSpy opcional")
    runtime_options(search_parser)
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--sites", default="indeed,google")
    search_parser.add_argument("--location", default="")
    search_parser.add_argument("--results-wanted", type=int, default=20)
    search_parser.set_defaults(handler="search")

    discover_parser = sub.add_parser(
        "discover",
        help="descobre vagas em APIs públicas de boards de ATS",
        description="GET somente leitura nas APIs JSON públicas de Greenhouse, Lever e Ashby. Sem credenciais e sem scraping.",
    )
    runtime_options(discover_parser)
    discover_parser.add_argument("--provider", required=True, choices=["greenhouse", "lever", "ashby"])
    discover_parser.add_argument("--board", action="append", required=True, help="token do board; pode repetir")
    discover_parser.add_argument("--query", default="", help="filtra por termos no título e na descrição")
    discover_parser.add_argument("--location", default="")
    discover_parser.add_argument("--limit", type=int, default=0)
    discover_parser.add_argument("--timeout", type=float, default=None)
    discover_parser.set_defaults(handler="discover")

    analyze_parser = sub.add_parser("analyze", help="analisa uma vaga persistida")
    runtime_options(analyze_parser)
    analyze_parser.add_argument("job_id")
    analyze_parser.set_defaults(handler="analyze")

    prepare_parser = sub.add_parser("prepare", help="gera currículo e artefatos")
    runtime_options(prepare_parser)
    prepare_parser.add_argument("job_id")
    prepare_parser.set_defaults(handler="prepare")

    application = sub.add_parser("application", help="prepara e consulta uma candidatura sem submissão")
    application_sub = application.add_subparsers(dest="application_command")
    application_prepare = application_sub.add_parser("prepare", help="cria ou retoma uma Application")
    runtime_options(application_prepare)
    application_prepare.add_argument("job_id")
    application_prepare.set_defaults(handler="application_prepare")
    application_resume = application_sub.add_parser("resume", help="retoma uma Application após intervenção humana")
    runtime_options(application_resume)
    application_resume.add_argument("application_id")
    application_resume.set_defaults(handler="application_resume")
    application_retry = application_sub.add_parser("retry-submit", help="reabre uma Application apos falha definitiva de submissao")
    runtime_options(application_retry)
    application_retry.add_argument("application_id")
    application_retry.set_defaults(handler="application_retry_submit")
    application_handoff = application_sub.add_parser(
        "handoff",
        help="monta o pacote de handoff humano e inicia o handoff",
        description=(
            "Monta, valida e persiste o pacote que o humano precisa para terminar a "
            "candidatura e so entao move a Application para HANDOFF_IN_PROGRESS. Se "
            "qualquer passo falhar, o estado continua NEEDS_HUMAN_CAPTCHA."
        ),
    )
    runtime_options(application_handoff)
    application_handoff.add_argument("application_id")
    application_handoff.set_defaults(handler="application_handoff")
    application_report = application_sub.add_parser(
        "report-manual-submit",
        help="registra o relato de envio manual (nunca marca SUBMITTED)",
        description=(
            "Registra MANUAL_SUBMISSION_REPORTED e move de HANDOFF_IN_PROGRESS para "
            "AWAITING_SUBMISSION_CONFIRMATION. O relato cria a incerteza; nao a "
            "satisfaz. SUBMITTED exige evidencia independente."
        ),
    )
    runtime_options(application_report)
    application_report.add_argument("application_id")
    application_report.set_defaults(handler="application_report_manual_submit")
    application_reconcile = application_sub.add_parser(
        "reconcile-confirmation",
        help="procura evidencia independente de confirmacao (somente leitura)",
        description=(
            "Roda os observadores de confirmacao sobre a janela aberta por "
            "MANUAL_SUBMISSION_REPORTED, registra o que foi observado e submete ao "
            "portao do dominio. Falha da fonte de dados NUNCA significa ausencia "
            "de confirmacao: a Application nao muda de estado."
        ),
    )
    runtime_options(application_reconcile)
    application_reconcile.add_argument("application_id")
    application_reconcile.add_argument("--gmail-dir", default=None, help="diretorio das credenciais do Gmail")
    application_reconcile.add_argument("--max-messages", type=int, default=None)
    application_reconcile.set_defaults(handler="application_reconcile_confirmation")
    application_status = application_sub.add_parser("status", help="consulta uma Application")
    runtime_options(application_status)
    application_status.add_argument("application_id")
    application_status.set_defaults(handler="application_status")
    application_review = application_sub.add_parser("review", help="cria ou consulta o Review Snapshot")
    runtime_options(application_review)
    application_review.add_argument("application_id")
    application_review.add_argument("--provider")
    application_review.add_argument("--destination")
    application_review.add_argument("--form-fingerprint")
    application_review.add_argument("--resume-sha256")
    application_review.add_argument("--answers-fingerprint")
    application_review.add_argument("--resume-filename", default="resume.pdf")
    application_review.add_argument("--expires-in", type=int, default=300)
    application_review.set_defaults(handler="application_review")
    application_authorize = application_sub.add_parser("authorize-submit", help="autoriza uma SubmissionIntent existente")
    runtime_options(application_authorize)
    application_authorize.add_argument("intent_id")
    application_authorize.set_defaults(handler="application_authorize")
    application_submit = application_sub.add_parser("submit", help="executa uma submissão Greenhouse autorizada")
    runtime_options(application_submit)
    application_submit.add_argument("application_id")
    application_submit.add_argument("--intent-id", required=True)
    application_submit.add_argument("--payload-json", type=Path, help="campos extras de string sobrepostos ao formulário")
    application_submit.add_argument("--form-fingerprint")
    application_submit.add_argument("--resume-sha256")
    application_submit.add_argument("--answers-fingerprint")
    application_submit.add_argument("--timeout", type=float, default=10.0)
    application_submit.set_defaults(handler="application_submit")

    linkedin = sub.add_parser("linkedin", help="inspeção offline de fixtures LinkedIn")
    linkedin_sub = linkedin.add_subparsers(dest="linkedin_command")
    linkedin_inspect = linkedin_sub.add_parser("inspect", help="classifica HTML local sem abrir o LinkedIn")
    runtime_options(linkedin_inspect)
    linkedin_inspect.add_argument("job_id")
    linkedin_inspect.add_argument("--html-file", type=Path, required=True)
    linkedin_inspect.set_defaults(handler="linkedin_inspect")

    run_parser = sub.add_parser("run", help="executa ingestão, análise e geração")
    runtime_options(run_parser)
    run_group = run_parser.add_mutually_exclusive_group(required=True)
    run_group.add_argument("--url")
    run_group.add_argument("--json-file", type=Path)
    run_parser.set_defaults(handler="run")

    preflight_parser = sub.add_parser("preflight", help="inspeciona uma vaga pública sem preencher ou submeter")
    runtime_options(preflight_parser)
    preflight_parser.add_argument("--url", required=True)
    preflight_parser.set_defaults(handler="preflight")

    precheck_parser = sub.add_parser("precheck", help="avalia fit e perfil antes de iniciar o browser")
    runtime_options(precheck_parser)
    precheck_parser.add_argument("job_id", help="ID de uma vaga já ingerida")
    precheck_parser.set_defaults(handler="precheck")

    dry_run_parser = sub.add_parser(
        "dry-run",
        help="executa preenchimento local sem rede e sem submit",
        description="Executa somente fill/upload em snapshot local; nunca submete.",
    )
    runtime_options(dry_run_parser)
    dry_run_parser.add_argument("job_id", help="ID da Application persistida")
    dry_run_parser.add_argument("--html-file", type=Path, required=True, help="snapshot HTML local")
    dry_run_parser.add_argument("--provider", choices=["greenhouse", "linkedin"], required=True)
    dry_run_parser.set_defaults(handler="dry_run")

    apply_parser = sub.add_parser(
        "apply",
        help="abre a vaga real, preenche, sobe o currículo e para antes do submit",
        description="Navega até a URL da vaga com sessão guardada (escrita de rede bloqueada), preenche os campos, sobe o currículo gerado e para. Use --submit para autorizar e executar UM POST.",
    )
    runtime_options(apply_parser)
    apply_parser.add_argument("job_id", help="ID de uma vaga já ingerida (usa a URL da vaga)")
    apply_parser.add_argument("--no-headless", dest="headless", action="store_false", default=True)
    apply_parser.add_argument("--no-advance", dest="advance", action="store_false", default=True, help="não clicar em Next/Continue")
    apply_parser.add_argument("--max-cycles", type=int, default=5)
    apply_parser.add_argument("--submit", action="store_true", help="após preencher, autoriza e executa UM POST controlado")
    apply_parser.add_argument("--timeout", type=float, default=10.0)
    apply_parser.add_argument("--intent-ttl", type=int, default=300, help="validade da autorização em segundos")
    apply_parser.set_defaults(handler="apply")

    status = sub.add_parser("status", help="lista vagas e estados")
    runtime_options(status)
    status.set_defaults(handler="status")

    integrations = sub.add_parser("integrations", help="acesso a servicos externos (opcional)")
    integrations_sub = integrations.add_subparsers(dest="integrations_command")
    gmail = integrations_sub.add_parser("gmail", help="acesso somente leitura a caixa do Gmail")
    gmail_sub = gmail.add_subparsers(dest="gmail_command")
    gmail_authorize = gmail_sub.add_parser(
        "authorize",
        help="estabelece o acesso (escopo gmail.readonly); nao confirma candidatura",
    )
    gmail_authorize.add_argument("--gmail-dir", default=None)
    gmail_authorize.add_argument("--no-browser", dest="open_browser", action="store_false", default=True)
    gmail_authorize.set_defaults(handler="gmail_authorize")
    gmail_status = gmail_sub.add_parser("status", help="estado da credencial, sem revelar segredo")
    gmail_status.add_argument("--gmail-dir", default=None)
    gmail_status.set_defaults(handler="gmail_status")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_args(args.root, db=args.db, artifacts=args.artifacts, profile=args.profile, facts=args.facts, preferences=args.preferences, answers=args.answers, application_policy=args.application_policy, real_profile=args.real_profile)


def _print(value: object) -> None:
    print(json.dumps(to_dict(value), ensure_ascii=False, indent=2))


def _application_view(application: object) -> dict[str, str]:
    """Projecao segura de uma Application para saida de comando.

    `context.answers` carrega nome, e-mail e telefone do candidato. Quem precisa
    disso usa `application status` e sabe que esta inspecionando PII; os comandos
    de handoff nao devolvem respostas ao terminal.
    """
    return {
        "id": str(getattr(application, "id", "")),
        "job_id": str(getattr(application, "job_id", "")),
        "state": str(getattr(getattr(application, "state", ""), "value", "")),
        "updated_at": str(getattr(application, "updated_at", "")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 0
    settings = _settings(args)
    try:
        if args.handler == "profile_validate":
            profile = load_profile(settings.resolve(settings.profile_path))
            facts = load_facts(settings.resolve(settings.facts_path))
            errors = validate_facts(profile, facts)
            if settings.real_profile and profile.demo:
                errors.append("demo profile cannot be used with --real-profile")
            _print({
                "valid": not errors,
                "profile_kind": "demo" if profile.demo else "candidate",
                "experience_count": len(profile.experiences),
                "education_count": len(profile.education),
                "facts": len(facts),
                "errors": errors,
            })
            return 0 if not errors else 2
        if args.handler == "profile_readiness":
            profile = load_profile(settings.resolve(settings.profile_path))
            facts = load_facts(settings.resolve(settings.facts_path))
            preferences = load_preferences(settings.resolve(settings.preferences_path), profile.preferences)
            profile.candidate_preferences = preferences
            fact_errors = validate_facts(profile, facts)
            readiness = validate_profile_readiness(profile, preferences)
            blockers = list(readiness.blockers)
            if fact_errors:
                blockers.append("invalid_locked_facts")
            ready = readiness.ready and not fact_errors
            _print({
                "ready": ready,
                "profile_kind": "demo" if profile.demo else "candidate",
                "policy_status": "BLOCKED" if profile.demo else "PASS",
                "policy_blockers": ["DEMO_PROFILE_BLOCKED"] if profile.demo else [],
                "missing_required": blockers,
                "missing_optional": readiness.missing_optional,
                "fact_errors": fact_errors,
            })
            return 0 if ready else 2
        if args.handler == "ingest":
            result = ingest_url(settings, args.url) if args.url else ingest(settings, json.loads(args.json_file.read_text(encoding="utf-8")), "")
            _print(result)
            return 0
        if args.handler == "search":
            _print(search(settings, args.query, sites=[item.strip() for item in args.sites.split(",") if item.strip()], location=args.location, results_wanted=args.results_wanted))
            return 0
        if args.handler == "discover":
            result = discover(
                settings,
                args.provider,
                args.board,
                query=args.query,
                location=args.location,
                limit=args.limit,
                timeout=args.timeout,
            )
            _print(result)
            return 0 if result["count"] or not result["failures"] else 2
        if args.handler == "analyze":
            _print(analyze(settings, args.job_id, args.language))
            return 0
        if args.handler == "precheck":
            result = precheck_job(settings, args.job_id)
            _print(result)
            return 0 if result["ready_for_dry_run"] else 2
        if args.handler == "dry_run":
            result = dry_run_application(settings, args.job_id, args.html_file, args.provider)
            _print(result)
            return 0 if result["status"] == "STOP_BEFORE_SUBMIT" else 2
        if args.handler == "apply":
            result = apply_live(
                settings,
                args.job_id,
                headless=args.headless,
                allow_advance=args.advance,
                max_cycles=args.max_cycles,
                submit=args.submit,
                submit_timeout=args.timeout,
                intent_ttl_seconds=args.intent_ttl,
            )
            _print(result)
            if args.submit:
                return 0 if result.get("submission", {}).get("status") == "SUBMITTED" else 2
            return 0 if result["status"] == "FILLED_REVIEW_REQUIRED" else 2
        if args.handler == "prepare":
            _print(prepare(settings, args.job_id, args.language))
            return 0
        if args.handler == "application_prepare":
            _print(prepare_application(settings, args.job_id, args.language))
            return 0
        if args.handler == "application_retry_submit":
            db = Database(settings.resolve(settings.db_path))
            try:
                application = ApplicationService(db).retry_submit(args.application_id)
                _print({"application": application, "events": db.list_application_events(application.id)})
            finally:
                db.close()
            return 0
        if args.handler == "application_resume":
            _print(resume_application(settings, args.application_id))
            return 0
        if args.handler == "application_handoff":
            db = Database(settings.resolve(settings.db_path))
            try:
                # Casca fina: quem sabe montar, validar e persistir o pacote e o
                # servico de dominio. O CLI so imprime a visao segura.
                package = HumanHandoffService(
                    db, settings.resolve(settings.artifacts_dir)
                ).prepare_handoff(args.application_id)
                application = db.get_application(args.application_id)
                _print({
                    "handoff": package.safe_view(),
                    # Projecao deliberada. Imprimir a Application inteira levava
                    # o `context.answers` para o terminal — exatamente o que a
                    # visao segura do pacote existe para nao fazer.
                    "application": _application_view(application),
                    "events": db.list_application_events(args.application_id),
                })
            finally:
                db.close()
            return 0
        if args.handler == "application_report_manual_submit":
            db = Database(settings.resolve(settings.db_path))
            try:
                application = ApplicationService(db).report_manual_submission(args.application_id)
                _print({
                    "application": _application_view(application),
                    "events": db.list_application_events(application.id),
                    # Dito no proprio resultado: quem le o comando nao pode
                    # confundir "relatei" com "confirmado".
                    "next": "submission confirmation requires independent evidence",
                })
            finally:
                db.close()
            return 0
        if args.handler == "linkedin_inspect":
            html = args.html_file.read_text(encoding="utf-8")
            inspection = LinkedInInspector().inspect_html(html, form_id=args.job_id)
            _print({
                "job_id": args.job_id,
                "classification": inspection.classification,
                "inspection": inspection,
                "network_access": "none",
            })
            return 0 if inspection.classification.value != "AUTH_REQUIRED" else 2
        if args.handler == "run":
            payload = None if args.url else json.loads(args.json_file.read_text(encoding="utf-8"))
            _print(run(settings, payload, args.url or "", args.language))
            return 0
        if args.handler == "preflight":
            result = run_preflight(args.url, settings.resolve(settings.artifacts_dir))
            _print(result)
            return 0 if result.status in {"READY", "DOM_UNSTABLE", "UNSUPPORTED_FORM", "UNSUPPORTED_PROVIDER"} else 2
        if args.handler == "application_reconcile_confirmation":
            from .confirmation import ConfirmationReconciliationService
            from .integrations.email import GmailApiClient, GmailEmailSource, GmailPaths, load_credentials
            from .integrations.email.gmail import DEFAULT_MAX_MESSAGES

            paths = GmailPaths.default(args.gmail_dir)
            credentials = load_credentials(paths)
            db = Database(settings.resolve(settings.db_path))
            try:
                result = ConfirmationReconciliationService(db).reconcile(
                    args.application_id,
                    email_sources=[
                        GmailEmailSource(
                            GmailApiClient(credentials),
                            max_messages=args.max_messages or DEFAULT_MAX_MESSAGES,
                        )
                    ],
                )
                _print({
                    "reconciliation": result.to_dict(),
                    "application": _application_view(db.get_application(args.application_id)),
                })
                return 0 if result.accepted else 2
            finally:
                db.close()
        if args.handler == "gmail_authorize":
            from .integrations.email import GmailPaths, authorize

            summary = authorize(GmailPaths.default(args.gmail_dir), open_browser=args.open_browser)
            _print({**summary, "scope": GMAIL_READONLY_SCOPE, "confirms_applications": False})
            return 0
        if args.handler == "gmail_status":
            from .integrations.email import GmailPaths, credentials_summary

            _print(credentials_summary(GmailPaths.default(args.gmail_dir)))
            return 0
        if args.handler == "status":
            db = Database(settings.resolve(settings.db_path))
            try:
                _print({"jobs": [to_dict(job) for job in db.list_jobs()]})
            finally:
                db.close()
            return 0
        if args.handler == "application_status":
            db = Database(settings.resolve(settings.db_path))
            try:
                application = db.get_application(args.application_id)
                if not application:
                    raise ApplicationDomainError(f"application not found: {args.application_id}")
                _print({"application": application, "events": db.list_application_events(application.id), "answers": db.list_application_answers(application.id), "form": db.get_application_form(application.id), "review_snapshot": db.get_review_snapshot(application.id), "submission_attempts": db.list_submission_attempts(application.id)})
            finally:
                db.close()
            return 0
        if args.handler == "application_review":
            db = Database(settings.resolve(settings.db_path))
            try:
                application = db.get_application(args.application_id)
                if not application:
                    raise ApplicationDomainError(f"application not found: {args.application_id}")
                snapshot = db.get_review_snapshot(application.id)
                supplied = [args.destination, args.form_fingerprint, args.resume_sha256, args.answers_fingerprint]
                if snapshot is None or any(value is not None for value in supplied):
                    job = db.get_job(application.job_id)
                    if not job:
                        raise ApplicationDomainError(f"job not found: {application.job_id}")
                    provider = args.provider or job.source
                    dry_run = application.context.get("validation", {}).get("dry_run", {})
                    destination = args.destination or job.url
                    form_fingerprint = args.form_fingerprint or dry_run.get("form_fingerprint")
                    resume_sha256 = args.resume_sha256 or application.context.get("resume_sha256")
                    answers_fingerprint = args.answers_fingerprint or dry_run.get("answers_fingerprint")
                    if not all((destination, form_fingerprint, resume_sha256, answers_fingerprint)):
                        raise SubmissionBoundaryError(
                            "review intent requires destination, form fingerprint, resume SHA256 and answers fingerprint"
                        )
                    form = db.get_application_form(application.id)
                    resolved_fields, manual_questions = review_field_rows(form) if form else ([], [])
                    snapshot = build_review_snapshot(
                        application_id=application.id,
                        job_id=application.job_id,
                        company=job.company,
                        title=job.title,
                        provider=provider,
                        destination=destination,
                        resume_filename=args.resume_filename,
                        resume_sha256=resume_sha256,
                        form_fingerprint=form_fingerprint,
                        answers_fingerprint=answers_fingerprint,
                        resolved_fields=resolved_fields,
                        manual_questions=manual_questions,
                    )
                    service = SubmissionService(db)
                    service.save_review_snapshot(snapshot)
                    intent = service.create_intent(
                        application_id=application.id,
                        job_id=application.job_id,
                        provider=provider,
                        destination=destination,
                        form_fingerprint=form_fingerprint,
                        resume_sha256=resume_sha256,
                        answers_fingerprint=answers_fingerprint,
                        expires_in_seconds=args.expires_in,
                    )
                else:
                    intent = None
                _print({"application": db.get_application(application.id), "review_snapshot": snapshot, "submission_intent": intent})
            finally:
                db.close()
            return 0
        if args.handler == "application_authorize":
            db = Database(settings.resolve(settings.db_path))
            try:
                intent = SubmissionService(db).authorize_submission(args.intent_id)
                _print({"submission_intent": intent, "application": db.get_application(intent.application_id), "events": db.list_application_events(intent.application_id)})
            finally:
                db.close()
            return 0
        if args.handler == "application_submit":
            db = Database(settings.resolve(settings.db_path))
            try:
                intent = db.get_submission_intent(args.intent_id)
                if not intent or intent.application_id != args.application_id:
                    raise SubmissionBoundaryError("submission intent does not belong to application")
                application = db.get_application(args.application_id)
                context = context_from_dict(application.context)
                form = context.form
                if form is None:
                    raise SubmissionBoundaryError("submission requires a persisted application form")
                built = build_submission_payload(
                    form,
                    artifact_root=str(settings.resolve(settings.artifacts_dir) / application.job_id),
                )
                fields: dict = dict(built.fields)
                if args.payload_json is not None:
                    extra = json.loads(args.payload_json.read_text(encoding="utf-8"))
                    if not isinstance(extra, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in extra.items()):
                        raise SubmissionBoundaryError("payload JSON must be an object with string keys and values")
                    fields.update(extra)
                live = application.context.get("validation", {}).get("dry_run", {})
                form_fingerprint = args.form_fingerprint or live.get("form_fingerprint")
                answers_fingerprint = args.answers_fingerprint or live.get("answers_fingerprint")
                resume_sha256 = args.resume_sha256 or application.context.get("resume_sha256")
                if not all((form_fingerprint, answers_fingerprint, resume_sha256)):
                    raise SubmissionBoundaryError(
                        "submission requires form fingerprint, answers fingerprint and resume SHA256"
                    )
                policy = LiveNetworkPolicy.for_submission("greenhouse", args.application_id, intent.id)
                result = GreenhouseSubmissionExecutor(db, timeout=args.timeout).submit(
                    intent.id,
                    current_form_fingerprint=form_fingerprint,
                    current_resume_sha256=resume_sha256,
                    current_answers_fingerprint=answers_fingerprint,
                    policy=policy,
                    fields=fields,
                    files=built.files,
                )
                attempts = db.list_submission_attempts(args.application_id)
                _print({"result": result, "application": db.get_application(args.application_id), "attempts": attempts})
                return 0 if result.status == "SUBMITTED" else 2
            finally:
                db.close()
    except (ApplicationConflict, ApplicationDomainError, SubmissionBoundaryError, ProfileError, PipelineError, LLMError, SourceError, OSError, ValueError, json.JSONDecodeError, ConfirmationObservationError, ConfirmationSourceUnavailable, GmailAuthError, GmailConfigError) as exc:
        _print({"error": str(exc), "type": type(exc).__name__})
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
