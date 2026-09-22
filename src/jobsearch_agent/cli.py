"""CLI JSON-first para execução local e consumo pelo Hermes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .application import ApplicationDomainError
from .config import Settings
from .llm import LLMError
from .models import to_dict
from .persistence import ApplicationConflict, Database
from .pipeline import PipelineError, analyze, ingest, ingest_url, prepare, prepare_application, resume_application, run, search
from .preflight import run_preflight
from .profile import ProfileError, load_facts, load_profile, validate_facts
from .sources import SourceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobsearch-agent", description="Analisa vagas e prepara currículos grounded.")
    parser.add_argument("--version", action="version", version="jobsearch-agent 0.1.0")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="raiz do projeto")
    parser.add_argument("--db", default="data/jobsearch.db")
    parser.add_argument("--artifacts", default="data/applications")
    parser.add_argument("--profile", default="profile/career_profile.yaml")
    parser.add_argument("--facts", default="profile/locked_facts.yaml")
    parser.add_argument("--real-profile", action="store_true", help="recusa fixtures demo")
    parser.add_argument("--preferences", default="profile/preferences.yaml")
    parser.add_argument("--answers", default="profile/answers.yaml")
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
    application_status = application_sub.add_parser("status", help="consulta uma Application")
    runtime_options(application_status)
    application_status.add_argument("application_id")
    application_status.set_defaults(handler="application_status")

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

    status = sub.add_parser("status", help="lista vagas e estados")
    runtime_options(status)
    status.set_defaults(handler="status")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_args(args.root, db=args.db, artifacts=args.artifacts, profile=args.profile, facts=args.facts, preferences=args.preferences, answers=args.answers, application_policy=args.application_policy, real_profile=args.real_profile)


def _print(value: object) -> None:
    print(json.dumps(to_dict(value), ensure_ascii=False, indent=2))


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
            _print({"valid": not errors, "profile": to_dict(profile), "facts": len(facts), "errors": errors})
            return 0 if not errors else 2
        if args.handler == "ingest":
            result = ingest_url(settings, args.url) if args.url else ingest(settings, json.loads(args.json_file.read_text(encoding="utf-8")), "")
            _print(result)
            return 0
        if args.handler == "search":
            _print(search(settings, args.query, sites=[item.strip() for item in args.sites.split(",") if item.strip()], location=args.location, results_wanted=args.results_wanted))
            return 0
        if args.handler == "analyze":
            _print(analyze(settings, args.job_id, args.language))
            return 0
        if args.handler == "prepare":
            _print(prepare(settings, args.job_id, args.language))
            return 0
        if args.handler == "application_prepare":
            _print(prepare_application(settings, args.job_id, args.language))
            return 0
        if args.handler == "application_resume":
            _print(resume_application(settings, args.application_id))
            return 0
        if args.handler == "run":
            payload = None if args.url else json.loads(args.json_file.read_text(encoding="utf-8"))
            _print(run(settings, payload, args.url or "", args.language))
            return 0
        if args.handler == "preflight":
            result = run_preflight(args.url, settings.resolve(settings.artifacts_dir))
            _print(result)
            return 0 if result.status in {"READY", "DOM_UNSTABLE", "UNSUPPORTED_FORM", "UNSUPPORTED_PROVIDER"} else 2
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
                _print({"application": application, "events": db.list_application_events(application.id), "answers": db.list_application_answers(application.id), "form": db.get_application_form(application.id)})
            finally:
                db.close()
            return 0
    except (ApplicationConflict, ApplicationDomainError, ProfileError, PipelineError, LLMError, SourceError, OSError, ValueError, json.JSONDecodeError) as exc:
        _print({"error": str(exc), "type": type(exc).__name__})
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
