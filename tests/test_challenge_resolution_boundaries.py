"""Fronteira arquitetural do `challenge_resolution`.

O invariante que separa "olhos" de "mãos" não pode depender de disciplina de
quem escreve o próximo arquivo: ele é verificado aqui, no import graph.

O pacote de resolução NÃO PODE:

  - importar qualquer coisa de `jobsearch_agent` (a dependência é de mão única:
    o agente conhece a resolução, a resolução não conhece o agente);
  - importar `AuthorizedWrite`, `SubmissionIntent`, `NetworkWriteGuard` ou
    `LiveNetworkPolicy` — é o que garante, por tipo, que resolver um challenge
    não pode consumir o orçamento de submissão;
  - importar `playwright` diretamente (só o executor concreto, numa fase
    posterior, e atrás de um protocolo).

Este teste é o par executável do contrato declarado em `pyproject.toml`
(`[tool.importlinter]`), que roda o mesmo invariante como check estático.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = REPO_ROOT / "src" / "challenge_resolution"

FORBIDDEN_MODULE_PREFIXES: tuple[str, ...] = (
    "jobsearch_agent",
    "playwright",
)

FORBIDDEN_NAMES: tuple[str, ...] = (
    "AuthorizedWrite",
    "SubmissionIntent",
    "NetworkWriteGuard",
    "LiveNetworkPolicy",
    "SubmissionCoordinator",
)


def _python_files() -> list[Path]:
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert files, f"pacote não encontrado em {PACKAGE_ROOT}"
    return files


def _imports(path: Path) -> list[str]:
    """Todo alvo de import do arquivo: módulos e nomes importados."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module)
            found.extend(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("path", _python_files(), ids=lambda path: path.name)
def test_package_does_not_import_the_agent(path: Path) -> None:
    for target in _imports(path):
        for prefix in FORBIDDEN_MODULE_PREFIXES:
            assert not target.startswith(prefix), f"{path.name} importa {target}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda path: path.name)
def test_package_never_imports_write_boundary_types(path: Path) -> None:
    for target in _imports(path):
        for forbidden in FORBIDDEN_NAMES:
            assert target != forbidden, f"{path.name} importa {forbidden}"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids dos nós que são docstring — documentação, não código."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


def test_the_write_boundary_types_are_not_even_reachable_by_name() -> None:
    """Nem por `getattr`/`__import__` dinâmico.

    Docstring e comentário PODEM nomear o que é proibido — é assim que a
    fronteira fica documentada. O que não pode é o nome aparecer como CÓDIGO:
    um `ast.Name`, um `ast.Attribute` ou uma string usada em `getattr`.
    """
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    # `getattr(modulo, "AuthorizedWrite")` seria string solta.
                    names.append(node.value)
            for forbidden in FORBIDDEN_NAMES:
                assert not any(forbidden in name for name in names), (
                    f"{path.name} usa {forbidden} como código"
                )


def test_the_package_is_registered_for_packaging() -> None:
    """O pacote vive em `src/` e precisa entrar no wheel, não só no sys.path."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = "challenge_resolution"' in pyproject
    assert 'include = "jobsearch_agent"' in pyproject


def test_the_static_contract_is_declared_for_import_linter() -> None:
    """O contrato existe em dois lugares: aqui e no import-linter."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.importlinter]" in pyproject
    assert "challenge_resolution" in pyproject.split("[tool.importlinter]")[1]


def test_the_feature_flag_defaults_to_disabled() -> None:
    """A Fase 0 é invisível em runtime: a resolução começa desligada."""
    from jobsearch_agent.config import EnvironmentSettings

    assert EnvironmentSettings().challenge_resolution_enabled is False


def test_the_agent_does_not_import_the_package_yet() -> None:
    """Enquanto a flag está desligada, nenhum módulo do agente importa o pacote.

    Este teste é o que impede a Fase 0 de virar comportamento por acidente: no
    dia em que a integração acontecer (Fase 6), ele muda de propósito e com
    revisão — não por descuido.
    """
    agent_root = REPO_ROOT / "src" / "jobsearch_agent"
    offenders: list[Path] = []
    for path in agent_root.rglob("*.py"):
        # `mentions` nao serve: comentario e docstring PODEM citar o pacote
        # (a flag e o plano vivem neles). O que nao pode e importar.
        for target in _imports(path):
            if target == "challenge_resolution" or target.startswith("challenge_resolution."):
                offenders.append(path.relative_to(REPO_ROOT))
                break
    assert offenders == [], f"o agente já importa o pacote: {offenders}"
