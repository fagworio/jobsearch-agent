"""LinkedIn authentication handoff without credential handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..models import ApplicationState


class LinkedInAuthState(StrEnum):
    NEEDS_LOGIN = "NEEDS_LOGIN"
    NEEDS_MFA = "NEEDS_MFA"
    NEEDS_CAPTCHA = "NEEDS_CAPTCHA"
    AUTHENTICATED_MANUAL = "AUTHENTICATED_MANUAL"


@dataclass(frozen=True)
class LinkedInCapabilities:
    """Capabilities exposed to the rest of the agent.

    The negative automation flags are intentional: they make the provider
    boundary auditable and prevent a generic executor from treating LinkedIn
    as an ordinary login-capable provider.
    """

    manual_handoff: bool = True
    automated_login: bool = False
    automated_navigation: bool = False
    automated_submit: bool = False


@dataclass(frozen=True)
class ManualHandoff:
    provider: str
    state: LinkedInAuthState
    profile_path: str
    instruction: str


@dataclass
class LinkedInSession:
    """State for a user-authenticated, persistent browser profile.

    `profile_path` points to a browser profile selected by the user. This
    object never accepts or persists passwords, MFA codes, cookies, tokens, or
    browser page handles.
    """

    profile_path: Path
    state: LinkedInAuthState = LinkedInAuthState.NEEDS_LOGIN

    @property
    def authenticated(self) -> bool:
        return self.state == LinkedInAuthState.AUTHENTICATED_MANUAL

    @property
    def application_state(self) -> ApplicationState | None:
        return {
            LinkedInAuthState.NEEDS_LOGIN: ApplicationState.NEEDS_LOGIN,
            LinkedInAuthState.NEEDS_MFA: ApplicationState.NEEDS_MFA,
            LinkedInAuthState.NEEDS_CAPTCHA: ApplicationState.NEEDS_CAPTCHA,
            LinkedInAuthState.AUTHENTICATED_MANUAL: None,
        }[self.state]

    def manual_handoff(self) -> ManualHandoff:
        instructions = {
            LinkedInAuthState.NEEDS_LOGIN: "Abra o navegador visível e conclua o login manualmente.",
            LinkedInAuthState.NEEDS_MFA: "Conclua o MFA manualmente no navegador visível.",
            LinkedInAuthState.NEEDS_CAPTCHA: "Resolva o CAPTCHA manualmente no navegador visível.",
            LinkedInAuthState.AUTHENTICATED_MANUAL: "A sessão manual está autenticada; nenhuma credencial foi capturada.",
        }
        return ManualHandoff(
            provider="linkedin",
            state=self.state,
            profile_path=str(self.profile_path),
            instruction=instructions[self.state],
        )

    def mark_intervention(self, state: LinkedInAuthState) -> None:
        if state == LinkedInAuthState.AUTHENTICATED_MANUAL:
            raise ValueError("use mark_manual_authentication for authenticated state")
        self.state = state

    def mark_manual_authentication(self, *, require_intervention: bool = False) -> None:
        if require_intervention and self.state in {
            LinkedInAuthState.NEEDS_MFA,
            LinkedInAuthState.NEEDS_CAPTCHA,
        }:
            raise ValueError("manual browser intervention must be completed first")
        self.state = LinkedInAuthState.AUTHENTICATED_MANUAL

    def to_dict(self) -> dict[str, str]:
        """Return safe state metadata; credentials are not representable here."""
        return {
            "provider": "linkedin",
            "state": self.state.value,
            "profile_path": str(self.profile_path),
        }
