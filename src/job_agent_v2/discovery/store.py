"""Persistência local, redigida e determinística dos lotes de discovery."""

from __future__ import annotations

import json
from pathlib import Path

from .greenhouse import GreenhouseDiscoveryAdapter
from .models import DiscoveryMatch, DiscoveryMatrix, DiscoverySearchRun


def save_matrix(matrix: DiscoveryMatrix, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(matrix.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination


def load_matrix(path: str | Path) -> DiscoveryMatrix:
    """Carrega e valida um lote previamente persistido."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("matrix must contain a JSON object")
    provider = payload.get("provider")
    work_type = payload.get("work_type")
    raw_runs = payload.get("runs")
    if provider != "greenhouse":
        raise ValueError("matrix provider must be greenhouse")
    if not isinstance(work_type, list) or not all(isinstance(item, str) for item in work_type):
        raise ValueError("matrix work_type must be a string list")
    if not isinstance(raw_runs, list):
        raise ValueError("matrix runs must be a list")

    adapter = GreenhouseDiscoveryAdapter()
    runs: list[DiscoverySearchRun] = []
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            raise ValueError(f"runs[{index}] must be an object")
        family = raw_run.get("family")
        query = raw_run.get("query")
        if not isinstance(family, str) or not isinstance(query, str):
            raise ValueError(f"runs[{index}] family and query must be strings")
        raw_results = raw_run.get("results")
        if not isinstance(raw_results, dict):
            raise ValueError(f"runs[{index}].results must be an object")
        runs.append(DiscoverySearchRun(family, query, adapter.inspect(raw_results)))
    raw_matching = payload.get("matching", {})
    matches: list[DiscoveryMatch] = []
    match_profile = ""
    if raw_matching:
        if not isinstance(raw_matching, dict):
            raise ValueError("matrix matching must be an object")
        match_profile = raw_matching.get("profile", "")
        raw_matches = raw_matching.get("matches", [])
        if not isinstance(match_profile, str) or not isinstance(raw_matches, list):
            raise ValueError("matrix matching profile and matches are invalid")
        for raw_match in raw_matches:
            if not isinstance(raw_match, dict):
                raise ValueError("matrix matching entries must be objects")
            matches.append(DiscoveryMatch.from_dict(raw_match))
    return DiscoveryMatrix(provider, tuple(work_type), tuple(runs), tuple(matches), match_profile)
