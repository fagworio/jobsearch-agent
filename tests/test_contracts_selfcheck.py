"""Anel 5 — um contrato que nunca falha não testa nada.

Estes testes introduzem uma violação DELIBERADA e verificam que o anel
correspondente a detecta. Se alguém "consertar" um contrato quebrado removendo a
checagem, este arquivo cai.

Os arquivos de sonda são criados e removidos dentro do teste. Se o processo for
morto no meio, o arquivo fica — e aí os próprios anéis falham alto, que é o
comportamento correto.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "src" / "challenge_resolution"
CONTRACTS_FILE = PACKAGE / "_contracts_probe.py"
AST_FILE = PACKAGE / "_ast_probe.py"


def _tool(name: str) -> list[str]:
    """Invocação da ferramenta no MESMO interpretador que roda o teste.

    Um `subprocess` cru depende de PATH: o console script pode não estar
    visível (o venv não está no PATH), e o anel falharia por ambiente, não por
    violação. Preferimos o script ao lado de `sys.executable`.
    """
    candidate = Path(sys.executable).parent / name
    if candidate.is_file():
        return [str(candidate)]
    located = shutil.which(name)
    if located:
        return [located]
    pytest.skip(f"{name} não está disponível neste interpretador")


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)


def test_the_import_contract_detects_a_deliberate_violation() -> None:
    if CONTRACTS_FILE.exists():
        pytest.skip("sonda presente de uma execução interrompida")
    try:
        CONTRACTS_FILE.write_text(
            "from jobsearch_agent.browser import AuthorizedWrite\n"
            "probe: object | None = None\n"
            "_ = AuthorizedWrite\n",
            encoding="utf-8",
        )
        result = _run(_tool("lint-imports") + ["--no-cache"])
        assert result.returncode != 0, "o contrato não percebeu a violação deliberada"
        assert "challenge_resolution nao importa jobsearch_agent" in result.stdout
    finally:
        CONTRACTS_FILE.unlink(missing_ok=True)


def test_the_ast_ring_detects_a_sensitive_dataclass_field() -> None:
    if AST_FILE.exists():
        pytest.skip("sonda presente de uma execução interrompida")
    try:
        AST_FILE.write_text(
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True)\n"
            "class _Probe:\n"
            "    session_token: str = ''\n",
            encoding="utf-8",
        )
        result = _run([sys.executable, "-m", "pytest", "tests/test_architecture_boundaries.py", "-q", "-p", "no:cacheprovider"])
        assert result.returncode != 0, "o anel 2 não percebeu o campo sensível"
        assert "_Probe.session_token" in result.stdout
    finally:
        AST_FILE.unlink(missing_ok=True)


def test_the_runtime_ring_detects_a_dynamic_import() -> None:
    """O anel 4 é o único que pega import dinâmico. A sonda usa importlib."""
    if AST_FILE.exists():
        pytest.skip("sonda presente de uma execução interrompida")
    try:
        # No CORPO do módulo, de propósito: o anel 4 executa todo o código de
        # import do pacote, e é isso que ele consegue pegar.
        AST_FILE.write_text(
            "import importlib\n\n"
            "importlib.import_module('jobsearch_agent.submission')\n",
            encoding="utf-8",
        )
        result = _run([sys.executable, "-m", "pytest", "tests/test_runtime_module_isolation.py", "-q", "-p", "no:cacheprovider"])
        assert result.returncode != 0, "o anel 4 não percebeu o import dinâmico"
    finally:
        AST_FILE.unlink(missing_ok=True)
