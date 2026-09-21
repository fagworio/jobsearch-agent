"""Eventos JSONL redigidos para auditoria local."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE = ("password", "token", "secret", "cookie", "authorization", "api_key")


def append_event(root: str | Path, event: str, **fields: Any) -> None:
    safe = {key: value for key, value in fields.items() if not any(part in key.lower() for part in SENSITIVE)}
    path = Path(root) / "work" / "telemetry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **safe}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

