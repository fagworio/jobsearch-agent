"""Manual LinkedIn handoff contracts.

This package deliberately contains no login, browser automation, scraper, or
submission implementation. The user owns authentication in a visible browser.
"""

from .session import LinkedInAuthState, LinkedInCapabilities, LinkedInSession, ManualHandoff

__all__ = [
    "LinkedInAuthState",
    "LinkedInCapabilities",
    "LinkedInSession",
    "ManualHandoff",
]
