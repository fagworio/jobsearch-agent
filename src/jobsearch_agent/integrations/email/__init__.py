"""Porto de caixa de e-mail: Gmail hoje, outros provedores depois.

O nucleo (`jobsearch_agent.confirmation`) so conhece `EmailSource` e
`EmailMessage`. Este pacote e uma IMPLEMENTACAO — trocar de provedor nao deve
tocar no observador, no matching nem no portao do dominio.
"""

from .gmail import GmailApiClient, GmailEmailSource
from .oauth import (
    ALLOWED_SCOPES,
    GMAIL_READONLY_SCOPE,
    GmailAuthError,
    GmailConfigError,
    GmailPaths,
    authorize,
    credentials_summary,
    load_credentials,
    load_token_document,
)

__all__ = [
    "ALLOWED_SCOPES",
    "GMAIL_READONLY_SCOPE",
    "GmailApiClient",
    "GmailAuthError",
    "GmailConfigError",
    "GmailEmailSource",
    "GmailPaths",
    "authorize",
    "credentials_summary",
    "load_credentials",
    "load_token_document",
]
