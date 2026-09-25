"""CLI minima do V2-001A: um comando, `apply`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .apply import apply


def _pairs(path: str) -> dict[str, str]:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return {str(key): str(value) for key, value in payload.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent-v2")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("apply", help="le a vaga real e decide (nao escreve)")
    run.add_argument("--url", required=True)
    run.add_argument("--approved", default="", help="JSON {prompt: answer} aprovado")
    run.add_argument("--profile", default="", help="JSON {campo trivial: valor}")
    args = parser.parse_args(argv)

    result = apply(args.url, approved=_pairs(args.approved), profile=_pairs(args.profile))
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.state.value == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
