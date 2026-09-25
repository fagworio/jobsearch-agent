"""Anel 4 — imports dinâmicos que o grafo estático não vê.

Um `importlib.import_module("jobsearch_agent.submission")` dentro do pacote de
resolução não aparece como `import` na AST. Este anel importa o pacote num
`sys.modules` limpo e verifica o que foi carregado de fato.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "jobsearch_agent",
    "playwright",
    "handoff",
)


def _import_every_module(package: str) -> tuple[list[str], set[str]]:
    """Importa o pacote E TODOS os submódulos, e devolve o que foi carregado.

    A verificação é por DIFERENÇA (`antes` vs `depois`), não por ausência global:
    outro teste da mesma sessão pode já ter importado `playwright`, e um anel que
    depende de ordem passa sozinho e falha na suíte — que é pior do que não
    existir. Importar só o `__init__` também não bastaria: um import dinâmico no
    corpo de um módulo só executa quando aquele módulo é importado.
    """
    before = set(sys.modules)
    imported = [package]
    importlib.import_module(package)
    package_file = importlib.import_module(package).__file__
    assert package_file is not None
    for module in pkgutil.walk_packages([str(Path(package_file).parent)], prefix=f"{package}."):
        importlib.import_module(module.name)
        imported.append(module.name)
    return imported, set(sys.modules) - before


def _leaks(loaded: set[str]) -> list[str]:
    return sorted(name for name in loaded if name.startswith(FORBIDDEN_PREFIXES))


def _unused_legacy_helper() -> list[str]:
    """Importa o pacote E TODOS os submódulos.

    Importar só o `__init__` deixaria passar um import dinâmico que vive no
    corpo de um módulo: ele só executa quando aquele módulo é importado. O anel
    só vale se executar todo o código de import do pacote.
    """
    imported = [package]
    importlib.import_module(package)
    for module in pkgutil.walk_packages([str(Path(importlib.import_module(package).__file__).parent)], prefix=f"{package}."):
        importlib.import_module(module.name)
        imported.append(module.name)
    return imported


@pytest.mark.parametrize("package", ["challenge_resolution"])
def test_importing_the_package_loads_nothing_from_the_agent(package: str) -> None:
    imported, loaded = _import_every_module(package)

    assert _leaks(loaded) == [], f"{package} ({len(imported)} módulos) carregou: {_leaks(loaded)}"


def test_importing_the_package_does_not_load_playwright() -> None:
    """O pacote de resolução não tem browser: nem por tabela."""
    _imported, loaded = _import_every_module("challenge_resolution")

    assert not [name for name in loaded if name.startswith("playwright")]


@pytest.mark.parametrize("package", ["challenge_resolution"])
def test_the_public_surface_is_exactly_what_all_declares(package: str) -> None:
    """Superfície pública mínima: `__all__` e nada além dele.

    Nomes de SUBMÓDULO (efeito colateral de `from .x import Y`) não contam como
    API pública — eles são o mecanismo de import, não a superfície.
    """
    module = importlib.import_module(package)
    declared = set(module.__all__)
    submodules = {
        name for name in vars(module)
        if getattr(getattr(module, name, None), "__name__", "").startswith(package)
    }
    exposed = {
        name for name in vars(module)
        if not name.startswith("_") and name not in submodules
    }

    assert exposed == declared, f"exposto sem declarar: {sorted(exposed - declared)}"
