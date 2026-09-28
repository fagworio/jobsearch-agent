"""Classificação de confirmação sem inferir sucesso por ausência de erro."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ConfirmationState(StrEnum):
    SUBMITTED = "SUBMITTED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"


@dataclass(frozen=True)
class ConfirmationEvidence:
    url: str
    confirmation_component: bool = False
    error_component: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ConfirmationResult:
    state: ConfirmationState
    primary: ConfirmationEvidence
    secondary: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "primary": {
                "url": self.primary.url,
                "confirmation_component": self.primary.confirmation_component,
                "error_component": self.primary.error_component,
                "detail": self.primary.detail,
            },
            "secondary": list(self.secondary),
        }


def classify_browser_result(payload: Mapping[str, Any]) -> ConfirmationResult:
    raw_primary = payload.get("primary")
    if not isinstance(raw_primary, Mapping):
        evidence = ConfirmationEvidence(url="")
        return ConfirmationResult(ConfirmationState.SUBMIT_UNKNOWN, evidence)
    evidence = ConfirmationEvidence(
        url=str(raw_primary.get("url") or ""),
        confirmation_component=raw_primary.get("confirmation_component") is True,
        error_component=raw_primary.get("error_component") is True,
        detail=str(raw_primary.get("detail") or ""),
    )
    if evidence.confirmation_component:
        state = ConfirmationState.SUBMITTED
    elif evidence.error_component:
        state = ConfirmationState.SUBMIT_FAILED
    else:
        state = ConfirmationState.SUBMIT_UNKNOWN
    secondary_raw = payload.get("secondary", ())
    secondary = tuple(item for item in secondary_raw if isinstance(item, str)) if isinstance(secondary_raw, (list, tuple)) else ()
    return ConfirmationResult(state, evidence, secondary)
