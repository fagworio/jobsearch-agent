"""Contratos de isolamento da V2, independentes da suíte legacy."""

from __future__ import annotations

import ast
from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "src" / "job_agent_v2"
FORBIDDEN = ("jobsearch_agent", "challenge_resolution", "challenge_guard")


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_v2_source_has_no_legacy_imports():
    violations: list[str] = []
    for path in V2_ROOT.rglob("*.py"):
        roots = _import_roots(ast.parse(path.read_text(encoding="utf-8")))
        for forbidden in FORBIDDEN:
            if forbidden in roots:
                violations.append(f"{path}: {forbidden}")
    assert violations == []


def test_importing_v2_does_not_load_legacy_packages():
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import job_agent_v2; import job_agent_v2.apply; "
                "import job_agent_v2.fill; import job_agent_v2.submit; "
                "assert not any(name.startswith(('jobsearch_agent', 'challenge_resolution', 'challenge_guard')) "
                "for name in sys.modules)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
