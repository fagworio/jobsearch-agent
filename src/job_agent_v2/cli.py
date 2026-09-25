"""CLI minima do V2: `apply` (decide), `fill` (preenche) e `submit` (envia)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .apply import apply
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
    args = parser.parse_args(argv)

    approved = _pairs(args.approved)
    profile = _pairs(args.profile)
    if args.command == "apply":
        result = apply(args.url, approved=approved, profile=profile)
    elif args.command == "fill":
        result = fill(args.url, approved=approved, profile=profile, resume=args.resume)
    else:
        result = submit(
            args.url,
            approved=approved,
            profile=profile,
            resume=args.resume,
            store=args.store,
            headless=not args.headful,
            human_wait_ms=args.human_wait,
        )

    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.state.value == "READY" or result.state.value == "SUBMITTED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
