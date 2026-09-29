"""CLI minima do V2: `apply` (decide), `fill` (preenche) e `submit` (envia)."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path

from .apply import apply
from .auto_apply import run_auto_apply
from .answers import AnswerLibrary
from .browser import NativeMessagingClient
from .fill import fill
from .facts import FactStore
from .facts_migration import load_approved_answers, migrate_answers
from .discovery import (
    DiscoveryMatrix,
    DiscoverySearchRun,
    GreenhouseDiscoveryAdapter,
    build_pipeline,
    build_query_matrix,
    load_match_profile,
    load_matrix,
    load_pipeline,
    load_policy,
    load_shortlist,
    match_matrix,
    plan_batch,
    rank_shortlist,
    SearchBudget,
    SearchCursor,
    save_batch,
    save_matrix,
    save_pipeline,
    save_shortlist,
)
from .submit import submit


def _pairs(path: str) -> dict[str, str]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return {str(key): str(value) for key, value in payload.items()}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", required=True)
    parser.add_argument("--approved", default="", help="caminho para JSON {prompt: resposta aprovada}")
    parser.add_argument("--profile", default="", help="caminho para JSON {campo trivial: valor}")
    parser.add_argument("--answers", default="", help="caminho para a biblioteca JSON de respostas aprovadas")
    parser.add_argument("--facts", default="profile/v2-facts.local.json", help="FactStore JSON de fatos aprovados")


def _library(path: str) -> AnswerLibrary | None:
    return AnswerLibrary.load(path) if path else None


def _batch_limits(path: str, overrides: dict[str, int | None]) -> dict[str, int]:
    policy = load_policy(path)
    greenhouse = ((policy.get("providers") or {}).get("greenhouse") or {})
    configured = greenhouse.get("batch") or {}
    defaults = {"max_jobs": 5, "max_submits": 3, "max_failures": 2, "parallelism": 1}
    limits: dict[str, int] = {}
    for name, default in defaults.items():
        value = overrides.get(name)
        if value is None:
            value = configured.get(name, default)
        if not isinstance(value, int):
            raise ValueError(f"greenhouse batch policy {name} must be an integer")
        limits[name] = value
    return limits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent-v2")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login-status", help="verifica o login manual no MyGreenhouse na aba ativa")
    sub.add_parser("discover", help="extrai os cards da busca MyGreenhouse ativa (somente leitura)")
    sub.add_parser("discover-filters", help="mapeia filtros e parâmetros da busca MyGreenhouse ativa (somente leitura)")
    discover_matrix = sub.add_parser("discover-matrix", help="executa a matriz de consultas remotas do perfil (somente leitura)")
    discover_matrix.add_argument("--store", default="data/v2-discovery/matrix.json", help="arquivo JSON do lote observado")
    discover_matrix.add_argument("--profile", default="profile/career_profile.local.yaml", help="Career Profile para parar ao atingir vagas prontas")
    discover_matrix.add_argument("--target-ready-jobs", type=int, default=3)
    discover_matrix.add_argument("--max-queries", type=int, default=20)
    discover_matrix.add_argument("--max-jobs-inspected", type=int, default=250)
    discover_matrix.add_argument("--max-pages", type=int, default=40)
    deduplicate_matrix = sub.add_parser("deduplicate-matrix", help="deduplica um lote salvo por job ID")
    deduplicate_matrix.add_argument("--source", default="data/v2-discovery/matrix.json", help="lote JSON de origem")
    deduplicate_matrix.add_argument("--store", default="", help="destino JSON; por padrão sobrescreve a origem atomicamente")
    classify_geo = sub.add_parser("classify-geo", help="classifica Brasil/LATAM/Worldwide em um lote salvo")
    classify_geo.add_argument("--source", default="data/v2-discovery/matrix.json", help="lote JSON de origem")
    classify_geo.add_argument("--store", default="", help="destino JSON; por padrão sobrescreve a origem atomicamente")
    match_matrix_parser = sub.add_parser("match-matrix", help="faz matching do lote contra o Career Profile local")
    match_matrix_parser.add_argument("--source", default="data/v2-discovery/matrix.json", help="lote JSON de origem")
    match_matrix_parser.add_argument("--profile", default="profile/career_profile.local.yaml", help="Career Profile YAML")
    match_matrix_parser.add_argument("--store", default="", help="destino JSON; por padrão sobrescreve a origem atomicamente")
    shortlist_parser = sub.add_parser("shortlist", help="ranqueia e justifica as vagas descobertas")
    shortlist_parser.add_argument("--source", default="data/v2-discovery/matrix.json", help="matriz JSON com matching")
    shortlist_parser.add_argument("--store", default="data/v2-discovery/shortlist.json", help="relatório JSON da shortlist")
    shortlist_parser.add_argument("--min-match-score", type=float, default=30.0)
    shortlist_parser.add_argument("--auto-approve-score", type=float, default=40.0)
    pipeline_parser = sub.add_parser("pipeline", help="entrega vagas aprovadas aos fluxos apply/fill/submit")
    pipeline_parser.add_argument("--source", default="data/v2-discovery/shortlist.json", help="shortlist JSON")
    pipeline_parser.add_argument("--store", default="data/v2-discovery/pipeline.json", help="manifest JSON do pipeline")
    pipeline_parser.add_argument("--submission-store", default="data/v2-submissions", help="marcadores que devem ser reconciliados antes de reencaminhar")
    batch_parser = sub.add_parser("batch", help="planeja o processamento autônomo seguro do pipeline")
    batch_parser.add_argument("--source", default="data/v2-discovery/pipeline.json", help="manifest JSON do pipeline")
    batch_parser.add_argument("--store", default="data/v2-discovery/batch.json", help="relatório JSON do lote")
    batch_parser.add_argument("--mode", choices=("plan", "apply", "fill", "submit"), default="plan")
    batch_parser.add_argument("--policy", default="profile/application_policy.yaml", help="política de autonomia")
    auto_parser = sub.add_parser("auto-apply", help="processa a shortlist aprovada sequencialmente com budgets explícitos")
    auto_parser.add_argument("--source", default="data/v2-discovery/pipeline.json", help="manifest do pipeline aprovado")
    auto_parser.add_argument("--resume", required=True, help="caminho do PDF a anexar")
    auto_parser.add_argument("--approved", default="", help="JSON {prompt: resposta aprovada}")
    auto_parser.add_argument("--profile", default="", help="JSON {campo trivial: valor}")
    auto_parser.add_argument("--rules", default="", help="JSON {prompt: resposta regida}")
    auto_parser.add_argument("--answers", default="", help="biblioteca JSON de respostas aprovadas")
    auto_parser.add_argument("--facts", default="profile/v2-facts.local.json", help="FactStore JSON de fatos aprovados")
    auto_parser.add_argument("--store", default="data/v2-submissions", help="diretório de marcadores de submit")
    auto_parser.add_argument("--report", default="data/v2-auto/auto-apply.json", help="relatório operacional")
    auto_parser.add_argument("--policy", default="profile/application_policy.yaml", help="política e budgets do lote")
    auto_parser.add_argument("--max-jobs", type=int, default=None)
    auto_parser.add_argument("--max-submits", type=int, default=None)
    auto_parser.add_argument("--max-failures", type=int, default=None)
    auto_parser.add_argument("--parallelism", type=int, default=None)
    auto_parser.add_argument("--human-wait", type=int, default=10 * 60 * 1000, help="ms para resolver desafio humano")
    decide = sub.add_parser("apply", help="le a vaga real e decide (nao escreve)")
    _common(decide)
    write = sub.add_parser("fill", help="preenche a vaga real e sobe o curriculo (nao submete)")
    _common(write)
    write.add_argument("--resume", default="", help="caminho do PDF a anexar")
    send = sub.add_parser("submit", help="preenche, sobe o curriculo e envia UMA vez")
    _common(send)
    send.add_argument("--resume", default="", help="caminho do PDF a anexar")
    send.add_argument("--store", default="data/v2-submissions", help="diretorio do marcador")
    send.add_argument("--headful", action="store_true", help="janela visivel (handoff humano)")
    send.add_argument("--human-wait", type=int, default=0, help="ms de sessao viva para uma pessoa")
    answers = sub.add_parser("answers", help="gerencia respostas aprovadas localmente")
    answers_sub = answers.add_subparsers(dest="answers_command", required=True)
    answer_set = answers_sub.add_parser("set", help="salva uma resposta explicitamente aprovada")
    answer_set.add_argument("--store", default="data/v2-answers.json")
    answer_set.add_argument("--prompt", required=True)
    answer_set.add_argument("--answer", required=True)
    migrate = sub.add_parser("migrate-facts", help="migra respostas aprovadas para fatos canônicos, sem inferência")
    migrate.add_argument("--answers", required=True, help="AnswerLibrary JSON de origem")
    migrate.add_argument("--facts", required=True, help="FactStore JSON de destino")
    args = parser.parse_args(argv)

    if args.command == "login-status":
        try:
            with NativeMessagingClient() as browser:
                result = browser.auth_state()
        except (OSError, RuntimeError) as exc:
            result = {"state": "UNAVAILABLE", "detail": str(exc)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("state") == "AUTHENTICATED_MANUAL" else 2

    if args.command == "discover":
        with NativeMessagingClient() as browser:
            result = GreenhouseDiscoveryAdapter().inspect(browser.inspect_discovery_results())
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "discover-filters":
        with NativeMessagingClient() as browser:
            result = GreenhouseDiscoveryAdapter().inspect_filters(browser.inspect_discovery_filters())
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "discover-matrix":
        adapter = GreenhouseDiscoveryAdapter()
        runs: list[DiscoverySearchRun] = []
        budget = SearchBudget(args.target_ready_jobs, args.max_queries, args.max_jobs_inspected, args.max_pages)
        budget.validate()
        cursor = SearchCursor()
        with NativeMessagingClient() as browser:
            initial = adapter.inspect(browser.inspect_discovery_results())
            work_type = initial.work_type or ("remote",)
            try:
                for item in build_query_matrix():
                    if not cursor.should_continue(budget):
                        break
                    snapshot = browser.discover_query(item.query, list(work_type))
                    runs.append(DiscoverySearchRun(item.family, item.query, adapter.inspect(snapshot)))
                    partial = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
                    cursor = SearchCursor(
                        queries_processed=len(runs),
                        jobs_inspected=partial.raw_job_count,
                        pages=len(runs),
                    )
                    if args.target_ready_jobs > 0:
                        matched = match_matrix(partial, load_match_profile(args.profile))
                        ready = rank_shortlist(matched, min_match_score=30.0, auto_approve_score=40.0)
                        ready_count = sum(
                            1 for entry in ready.entries
                            if entry.selection == "APPROVED" and not entry.applied
                        )
                        cursor = replace(cursor, ready_jobs=ready_count)
            finally:
                # Return the dedicated browser to the query the user had open.
                browser.discover_query(initial.query or "frontend", list(work_type))
        result = DiscoveryMatrix("greenhouse", work_type, tuple(runs))
        destination = save_matrix(result, args.store)
        print(json.dumps({
            "provider": result.provider,
            "work_type": list(result.work_type),
            "query_count": len(result.runs),
            "queries_processed": cursor.queries_processed,
            "jobs_inspected": cursor.jobs_inspected,
            "pages": cursor.pages,
            "ready_jobs": cursor.ready_jobs,
            "budgets": {
                "target_ready_jobs": budget.target_ready_jobs,
                "max_queries": budget.max_queries,
                "max_jobs_inspected": budget.max_jobs_inspected,
                "max_pages": budget.max_pages,
            },
            "job_count": result.raw_job_count,
            "unique_job_count": len(result.unique_jobs),
            "duplicate_job_count": result.raw_job_count - len(result.unique_jobs),
            "geography": result.to_dict()["geography"],
            "job_count_by_query": {run.query: len(run.results.jobs) for run in result.runs},
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "deduplicate-matrix":
        source = Path(args.source)
        result = load_matrix(source)
        destination = save_matrix(result, args.store or source)
        print(json.dumps({
            "provider": result.provider,
            "query_count": len(result.runs),
            "job_count": result.raw_job_count,
            "unique_job_count": len(result.unique_jobs),
            "duplicate_job_count": result.raw_job_count - len(result.unique_jobs),
            "geography": result.to_dict()["geography"],
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "classify-geo":
        source = Path(args.source)
        result = load_matrix(source)
        destination = save_matrix(result, args.store or source)
        print(json.dumps({
            "provider": result.provider,
            "query_count": len(result.runs),
            "unique_job_count": len(result.unique_jobs),
            "geography": result.to_dict()["geography"],
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "match-matrix":
        source = Path(args.source)
        result = load_matrix(source)
        matched = match_matrix(result, load_match_profile(args.profile))
        destination = save_matrix(matched, args.store or source)
        matching = matched.to_dict()["matching"]
        print(json.dumps({
            "provider": matched.provider,
            "query_count": len(matched.runs),
            "unique_job_count": len(matched.unique_jobs),
            "matching": {
                "profile": matching["profile"],
                "counts_by_band": matching["counts_by_band"],
            },
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "shortlist":
        source = Path(args.source)
        report = rank_shortlist(
            load_matrix(source),
            source=str(source),
            min_match_score=args.min_match_score,
            auto_approve_score=args.auto_approve_score,
        )
        destination = save_shortlist(report, args.store)
        payload = report.to_dict()
        print(json.dumps({
            "provider": report.provider,
            "counts": payload["counts"],
            "top": payload["entries"][:10],
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "pipeline":
        source = Path(args.source)
        manifest = build_pipeline(load_shortlist(source), submission_store=args.submission_store)
        destination = save_pipeline(manifest, args.store)
        print(json.dumps({
            "provider": "greenhouse",
            "source": str(source),
            "approved_count": len(manifest.items),
            "actions": ["apply", "fill", "submit"],
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "batch":
        source = Path(args.source)
        manifest = load_pipeline(source)
        report = plan_batch(manifest, mode=args.mode, policy=load_policy(args.policy))
        destination = save_batch(report, args.store)
        print(json.dumps({
            "source": str(source),
            "mode": report.mode,
            "counts": report.to_dict()["counts"],
            "store": str(destination),
        }, indent=2, ensure_ascii=False))
        return 0

    if args.command == "auto-apply":
        if not args.answers:
            print(json.dumps({"state": "CONFIGURATION_ERROR", "reason": "--answers is required for auto-apply"}, ensure_ascii=False))
            return 2
        try:
            batch_library = AnswerLibrary.load_required(args.answers)
            batch_facts = FactStore.load_required(args.facts)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"state": "CONFIGURATION_ERROR", "reason": str(exc)}, ensure_ascii=False))
            return 2
        limits = _batch_limits(args.policy, {
            "max_jobs": args.max_jobs,
            "max_submits": args.max_submits,
            "max_failures": args.max_failures,
            "parallelism": args.parallelism,
        })
        report = run_auto_apply(
            load_pipeline(args.source),
            resume=args.resume,
            approved=_pairs(args.approved),
            profile=_pairs(args.profile),
            rules=_pairs(args.rules),
            library=batch_library,
            facts=batch_facts,
            marker_store=args.store,
            report_store=args.report,
            max_jobs=limits["max_jobs"],
            max_submits=limits["max_submits"],
            max_failures=limits["max_failures"],
            parallelism=limits["parallelism"],
            human_wait_ms=args.human_wait,
        )
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0 if report.to_dict()["counts"].get("SUBMITTED", 0) else 2

    if args.command == "answers":
        library = AnswerLibrary.load(args.store)
        library.remember(args.prompt, args.answer)
        library.save(args.store)
        print(json.dumps({"saved": True, "store": args.store, "prompt": args.prompt}, ensure_ascii=False))
        return 0

    if args.command == "migrate-facts":
        answers = load_approved_answers(args.answers)
        facts = FactStore.load(args.facts)
        report = migrate_answers(answers, facts)
        facts.save(args.facts)
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0

    approved = _pairs(args.approved)
    profile = _pairs(args.profile)
    library = _library(args.answers)
    facts = FactStore.load(args.facts)
    if args.command == "apply":
        result = apply(args.url, approved=approved, profile=profile, library=library, facts=facts)
    elif args.command == "fill":
        result = fill(args.url, approved=approved, profile=profile, library=library, facts=facts, resume=args.resume)
    else:
        result = submit(
            args.url,
            approved=approved,
            profile=profile,
            library=library,
            facts=facts,
            resume=args.resume,
            store=args.store,
            headless=not args.headful,
            human_wait_ms=args.human_wait,
        )

    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.state.value == "READY" or result.state.value == "SUBMITTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
