"""Serialização canônica usada nos artefatos e hashes de auditoria."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import to_dict


def canonical_json(value: Any) -> str:
    return json.dumps(to_dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def jsonable(value: Any) -> Any:
    return json.loads(canonical_json(value))

