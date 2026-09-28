"""CLI minima do V2: `apply` (decide), `fill` (preenche) e `submit` (envia)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .apply import apply
from .answers import AnswerLibrary
from .fill import fill
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


def _library(path: str) -> AnswerLibrary | None:
    return AnswerLibrary.load(path) if path else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent-v2")
    sub = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args(argv)

    if args.command == "answers":
        library = AnswerLibrary.load(args.store)
        library.remember(args.prompt, args.answer)
        library.save(args.store)
        print(json.dumps({"saved": True, "store": args.store, "prompt": args.prompt}, ensure_ascii=False))
        return 0

    approved = _pairs(args.approved)
    profile = _pairs(args.profile)
    library = _library(args.answers)
    if args.command == "apply":
        result = apply(args.url, approved=approved, profile=profile, library=library)
    elif args.command == "fill":
        result = fill(args.url, approved=approved, profile=profile, library=library, resume=args.resume)
    else:
        result = submit(
            args.url,
            approved=approved,
            profile=profile,
            library=library,
            resume=args.resume,
            store=args.store,
            headless=not args.headful,
            human_wait_ms=args.human_wait,
        )

    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.state.value == "READY" or result.state.value == "SUBMITTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
