"""Guarda contra testes que passam localmente e falham no runner.

Um teste que le um arquivo ignorado pelo Git (``profile/*.local.yaml``, um
arquivo concreto de respostas) funciona na maquina de quem tem o arquivo e
quebra no CI, onde ele nao existe. Foi exatamente o que aconteceu: a suite dava
274 verdes localmente e o gate ``core-runtime`` falhava com
``FileNotFoundError: profile/career_profile.local.yaml``.

Em vez de corrigir so a ocorrencia, este modulo verifica a propriedade: nenhum
teste pode ler um caminho versionavel que o Git nao rastreia.

A guarda dispara onde o arquivo existe — isto e, na maquina de quem tem o dado
local, que e exatamente onde o erro nasce. No runner o arquivo nao existe, entao
ela e um no-op; la quem acusa e o proprio teste dependente, com
``FileNotFoundError``.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).parents[1]

#: Caminhos relativos ao repositorio citados como literal em codigo de teste.
_PATH_LITERAL = re.compile(r"""["']([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:yaml|yml))["']""")


def _path_literals(source: str) -> list[str]:
    return [match.group(1) for match in _PATH_LITERAL.finditer(source)]


def _tracked_files(root: Path) -> set[str] | None:
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def test_the_detector_recognizes_the_historical_offender():
    """A guarda so vale se realmente pegar o caso que quebrou o CI."""
    assert _path_literals('profile = load_profile(ROOT / "profile/career_profile.local.yaml")') == [
        "profile/career_profile.local.yaml"
    ]
    assert _path_literals('yaml.safe_load("profile/answers.yaml")') == ["profile/answers.yaml"]


def test_no_test_reads_a_file_that_git_does_not_track():
    tracked = _tracked_files(ROOT)
    if tracked is None:
        pytest.skip("git is not available to determine the tracked file set")

    offenders: list[str] = []
    for source in sorted((ROOT / "tests").rglob("*.py")):
        if source.name == Path(__file__).name:
            continue
        text = source.read_text(encoding="utf-8")
        for candidate in _path_literals(text):
            path = ROOT / candidate
            if path.is_file() and candidate not in tracked:
                offenders.append(f"{source.relative_to(ROOT)} reads untracked {candidate}")

    assert not offenders, (
        "testes dependem de arquivos que nao existem no CI "
        "(construa um fixture sintetico em vez de ler dado local):\n  " + "\n  ".join(offenders)
    )
