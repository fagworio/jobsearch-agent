from pathlib import Path

import pytest

from jobsearch_agent.linkedin.session import (
    LinkedInAuthState,
    LinkedInCapabilities,
    LinkedInSession,
    ManualHandoff,
)
from jobsearch_agent.models import ApplicationState


def test_linkedin_session_starts_needing_manual_login_without_credentials(tmp_path: Path):
    session = LinkedInSession(profile_path=tmp_path / "linkedin-profile")
    assert session.state == LinkedInAuthState.NEEDS_LOGIN
    assert session.application_state == ApplicationState.NEEDS_LOGIN
    assert session.authenticated is False
    assert not hasattr(session, "password")
    assert not hasattr(session, "login")
    handoff = session.manual_handoff()
    assert isinstance(handoff, ManualHandoff)
    assert handoff.state == LinkedInAuthState.NEEDS_LOGIN
    assert "manualmente" in handoff.instruction.casefold()


def test_manual_mfa_and_captcha_states_map_to_application_interventions(tmp_path: Path):
    session = LinkedInSession(profile_path=tmp_path / "linkedin-profile")
    session.mark_intervention(LinkedInAuthState.NEEDS_MFA)
    assert session.application_state == ApplicationState.NEEDS_MFA
    assert session.manual_handoff().state == LinkedInAuthState.NEEDS_MFA
    session.mark_intervention(LinkedInAuthState.NEEDS_CAPTCHA)
    assert session.application_state == ApplicationState.NEEDS_CAPTCHA
    assert session.manual_handoff().state == LinkedInAuthState.NEEDS_CAPTCHA


def test_manual_authentication_is_explicit_and_only_exposes_safe_state(tmp_path: Path):
    session = LinkedInSession(profile_path=tmp_path / "linkedin-profile")
    session.mark_manual_authentication()
    assert session.state == LinkedInAuthState.AUTHENTICATED_MANUAL
    assert session.authenticated is True
    serialized = session.to_dict()
    assert serialized == {
        "provider": "linkedin",
        "state": "AUTHENTICATED_MANUAL",
        "profile_path": str(tmp_path / "linkedin-profile"),
    }
    assert "password" not in str(serialized).casefold()
    assert "token" not in str(serialized).casefold()


def test_linkedin_capabilities_forbid_automated_login_navigation_and_submit():
    capabilities = LinkedInCapabilities()
    assert capabilities.manual_handoff is True
    assert capabilities.automated_login is False
    assert capabilities.automated_navigation is False
    assert capabilities.automated_submit is False


def test_manual_authentication_cannot_be_called_from_invalid_state(tmp_path: Path):
    session = LinkedInSession(profile_path=tmp_path / "linkedin-profile")
    session.mark_intervention(LinkedInAuthState.NEEDS_CAPTCHA)
    with pytest.raises(ValueError, match="manual browser intervention"):
        session.mark_manual_authentication(require_intervention=True)
