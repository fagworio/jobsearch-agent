"""Anel 2 — o que o import-linter NÃO vê.

O import-linter trabalha sobre o grafo de imports. Ele não vê:

  - um nome proibido usado como código sem import (`getattr(mod, "X")`);
  - um nome proibido exportado em `__all__`;
  - campo sensível em dataclass;
  - tipo proibido em anotação (forward reference em string);
  - `jobsearch_agent` reexportando internals dos pacotes protegidos;
  - o artefato EXTERNO (`challenge_guard`) importando o nosso código.

Este arquivo é ele mesmo um contrato. Se precisar de exceção, ela entra explícita
e justificada — nunca removendo a checagem.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

#: Pacotes e módulos sob vigilância (nossos).
PROTECTED_SOURCES: tuple[Path, ...] = (
    SRC / "challenge_resolution",
    SRC / "jobsearch_agent" / "live_view.py",
    SRC / "jobsearch_agent" / "challenge_gate.py",
)

FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "AuthorizedWrite",
        "SubmissionIntent",
        "NetworkWriteGuard",
        "LiveNetworkPolicy",
    }
)

FORBIDDEN_FIELD_PATTERNS = re.compile(
    r"(token|cookie|secret|jwt|bearer|password|session_key|api_key|credential)",
    re.IGNORECASE,
)


def _python_files(source: Path) -> list[Path]:
    if source.is_dir():
        return sorted(source.rglob("*.py"))
    return [source] if source.is_file() else []


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Ids dos nós que são docstring: documentação pode NOMEAR o proibido."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    ids.add(id(body[0].value))
    return ids


@pytest.mark.parametrize("source", PROTECTED_SOURCES, ids=lambda path: path.name)
def test_no_forbidden_identifier_is_used_as_code(source: Path) -> None:
    """Nome proibido como `Name`, `Attribute` ou string fora de docstring."""
    violations: list[str] = []
    for path in _python_files(source):
        tree = _parse(path)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    names.append(node.value)
            for name in names:
                for forbidden in FORBIDDEN_IDENTIFIERS:
                    if forbidden in name:
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} usa {forbidden}")
    assert not violations, "identificadores proibidos como código:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize("source", PROTECTED_SOURCES, ids=lambda path: path.name)
def test_no_forbidden_name_in_dunder_all(source: Path) -> None:
    violations: list[str] = []
    for path in _python_files(source):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Assign):
                continue
            target = node.targets[0] if node.targets else None
            if not (isinstance(target, ast.Name) and target.id == "__all__"):
                continue
            if not isinstance(node.value, (ast.List, ast.Tuple)):
                continue
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and element.value in FORBIDDEN_IDENTIFIERS:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} __all__ tem {element.value!r}")
    assert not violations, "__all__ com nome proibido:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize("source", PROTECTED_SOURCES, ids=lambda path: path.name)
def test_no_sensitive_field_in_dataclasses(source: Path) -> None:
    violations: list[str] = []
    for path in _python_files(source):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef) or not _is_dataclass(node):
                continue
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    if FORBIDDEN_FIELD_PATTERNS.search(statement.target.id):
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{statement.lineno} {node.name}.{statement.target.id}")
    assert not violations, "campo sensível em dataclass:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize("source", PROTECTED_SOURCES, ids=lambda path: path.name)
def test_no_forbidden_type_in_annotations(source: Path) -> None:
    violations: list[str] = []
    for path in _python_files(source):
        for node in ast.walk(_parse(path)):
            for annotation, lineno in _annotations(node):
                for name in _annotation_names(annotation):
                    if name in FORBIDDEN_IDENTIFIERS:
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno} anotação com {name}")
    assert not violations, "tipo proibido em anotação:\n  " + "\n  ".join(violations)


def test_the_agent_does_not_reexport_protected_internals() -> None:
    path = SRC / "jobsearch_agent" / "__init__.py"
    if not path.is_file():
        pytest.skip("jobsearch_agent/__init__.py não existe")
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0] if node.targets else None
        if not (isinstance(target, ast.Name) and target.id == "__all__"):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        for element in node.value.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                assert not element.value.startswith("challenge_resolution")


def test_the_pinned_guard_never_imports_our_code() -> None:
    """`challenge_guard` é o FUNDO da pilha: percepção pura.

    Ele é um artefato externo fixado por SHA, então a checagem é sobre o que está
    instalado — não sobre o nosso fonte. Se o guard passar a importar o agente, a
    direção da pilha se inverte e este teste cai antes do import-linter.
    """
    guard_root = Path(sysconfig.get_paths()["purelib"]) / "challenge_guard"
    if not guard_root.is_dir():
        pytest.skip("challenge_guard não instalado")
    violations: list[str] = []
    for path in sorted(guard_root.rglob("*.py")):
        for target in _import_targets(_parse(path)):
            if target.startswith(("jobsearch_agent", "challenge_resolution")):
                violations.append(f"{path.name} importa {target}")
    assert not violations, "o guard passou a importar camadas de cima:\n  " + "\n  ".join(violations)


def test_the_architecture_doc_lists_every_configured_layer() -> None:
    """Diagrama vivo: a documentação não pode divergir do contrato."""
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    layers = next(contract["layers"] for contract in contracts if contract["type"] == "layers")
    doc = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    for layer in layers:
        assert layer in doc, f"camada {layer} não aparece em docs/architecture.md"
    assert "não importa" in doc or "nao importa" in doc


# -- helpers -----------------------------------------------------------------


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
            return True
        if isinstance(decorator, ast.Call):
            function = decorator.func
            if isinstance(function, ast.Name) and function.id == "dataclass":
                return True
            if isinstance(function, ast.Attribute) and function.attr == "dataclass":
                return True
    return False


def _annotations(node: ast.AST) -> list[tuple[ast.AST, int]]:
    found: list[tuple[ast.AST, int]] = []
    if isinstance(node, ast.AnnAssign) and node.annotation is not None:
        found.append((node.annotation, node.lineno))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.returns is not None:
            found.append((node.returns, node.lineno))
        arguments = list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
        for argument in arguments:
            if argument.annotation is not None:
                found.append((argument.annotation, argument.lineno))
        for extra in (node.args.vararg, node.args.kwarg):
            if extra is not None and extra.annotation is not None:
                found.append((extra.annotation, node.lineno))
    return found


def _annotation_names(annotation: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.extend(re.findall(r"\b\w+\b", node.value))
    return names


def _import_targets(tree: ast.Module) -> list[str]:
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append(node.module)
    return targets


#: O módulo precisa usar `dataclasses` para o helper de dataclass? Não — a
#: checagem é sintática (decorador `dataclass`). Este import existe só para
#: deixar claro que não usamos `dataclasses.fields` aqui.
_ = dataclasses
