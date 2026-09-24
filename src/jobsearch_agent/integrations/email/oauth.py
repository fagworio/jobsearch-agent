"""OAuth e credenciais locais do Gmail (JSA-CONF-006A / 006D).

**Least privilege.** O unico escopo pedido e `gmail.readonly`. O agente nao marca
como lido, nao move, nao exclui e nao envia: o escopo e a garantia MECANICA
disso, e nao uma convencao deste modulo. `gmail.metadata` sozinho nao serve,
porque a confirmacao exige ler o assunto e o corpo em memoria para encontrar a
frase de recebimento e as corroboracoes.

**Segredo local.** O refresh token e do usuario e fica fora do projeto:

    ~/.config/jobsearch-agent/gmail/
    ├── client_secret.json   OAuth client (configuracao da aplicacao)
    └── token.json           refresh/access token (segredo do usuario)

Diretorio `0700`, arquivos `0600` — verificados NA LEITURA, e nao apenas criados
assim. Um token que o grupo ou o mundo pode ler e recusado: se ele vazou, a
resposta certa e reautorizar, nao seguir usando.

Nada disto entra em `Application.context`, journal de eventos,
`confirmation_evidence`, artifacts, log ou stdout. `credentials_summary` existe
para o comando de status poder dizer "esta configurado" sem dizer o que esta
configurado.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

#: Escopos que o agente aceita carregar. Um token com escopo mais amplo e
#: recusado mesmo que contenha o readonly: se a credencial pode mais do que o
#: necessario, ela nao e a credencial deste sistema.
ALLOWED_SCOPES = frozenset({GMAIL_READONLY_SCOPE})

_DEFAULT_DIRECTORY = "~/.config/jobsearch-agent/gmail"
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


class GmailConfigError(ValueError):
    """Configuracao ausente ou insegura. Nada foi lido."""


class GmailAuthError(RuntimeError):
    """A credencial existe mas nao serve (expirada, refresh recusado, escopo)."""


@dataclass(frozen=True)
class GmailPaths:
    directory: Path
    client_secret: Path
    token: Path

    @classmethod
    def default(cls, directory: str | Path | None = None) -> "GmailPaths":
        raw = directory or os.environ.get("JOBSEARCH_GMAIL_DIR") or _DEFAULT_DIRECTORY
        base = Path(raw).expanduser()
        return cls(base, base / "client_secret.json", base / "token.json")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def ensure_private_directory(path: Path) -> None:
    """Cria com `0700` ou recusa um diretorio que outros podem ler."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(_DIRECTORY_MODE)
    except OSError:  # pragma: no cover - sistemas de arquivo sem chmod
        pass
    if _mode(path) & 0o077:
        raise GmailConfigError(
            f"credential directory is accessible to group/other: {path} "
            f"(mode {_mode(path):04o}); run chmod 700"
        )


def assert_private_file(path: Path) -> None:
    if not path.is_file():
        raise GmailConfigError(f"credential file is missing: {path}")
    if _mode(path) & 0o077:
        raise GmailConfigError(
            f"credential file is accessible to group/other: {path} "
            f"(mode {_mode(path):04o}); run chmod 600"
        )


def _write_private(path: Path, payload: str) -> None:
    """Escreve criando ja com `0600` (nunca um instante legivel por outros)."""
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    path.chmod(_FILE_MODE)


def load_token_document(path: Path) -> dict[str, Any]:
    """Le o token conferindo permissao e ESCOPO.

    Os dois portoes rodam sobre o JSON, sem importar biblioteca do Google: e o
    que permite testar a politica de privacidade sem credencial e sem rede.
    """
    assert_private_file(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GmailConfigError(f"credential file is not valid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise GmailConfigError(f"credential file has an unexpected shape: {path}")
    scopes = {str(scope) for scope in document.get("scopes", [])}
    if not scopes:
        raise GmailAuthError("credential file records no scopes")
    extra = scopes - ALLOWED_SCOPES
    if extra:
        raise GmailAuthError(
            "credential carries scopes beyond " + GMAIL_READONLY_SCOPE + f": {sorted(extra)}"
        )
    if not document.get("refresh_token") and not document.get("token"):
        raise GmailAuthError("credential file has neither access nor refresh token")
    # O `AuthorizedUserInfo` do google-auth exige estes dois; sem eles a
    # biblioteca levanta `ValueError: ... not in the expected format`, que nao diz
    # ao operador o que fazer. A checagem mora aqui para a mensagem ser acionavel
    # mesmo sem a biblioteca instalada.
    missing = [key for key in ("client_id", "client_secret") if not document.get(key)]
    if missing:
        raise GmailAuthError(
            f"credential file is missing {', '.join(missing)}; reauthorize with "
            "`integrations gmail authorize`"
        )
    return document


def _google_credentials_factory(document: dict[str, Any]) -> Any:
    try:  # pragma: no cover - depende da biblioteca opcional
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover
        raise GmailConfigError(
            "google-auth-oauthlib is not installed; "
            "install the optional group: pip install 'google-api-python-client' 'google-auth-oauthlib'"
        ) from exc
    try:  # pragma: no cover - depende da biblioteca opcional
        return Credentials.from_authorized_user_info(document, list(ALLOWED_SCOPES))
    except (ValueError, KeyError) as exc:  # pragma: no cover
        # A biblioteca tambem valida o formato; a mensagem dela nao diz o que
        # fazer, entao ela nao chega ao operador.
        raise GmailAuthError(
            "credential file is not a usable authorized-user document; "
            "reauthorize with `integrations gmail authorize`"
        ) from exc


def _google_request_factory() -> Any:
    try:  # pragma: no cover - depende da biblioteca opcional
        from google.auth.transport.requests import Request
    except ImportError as exc:  # pragma: no cover
        raise GmailConfigError("google-auth is not installed") from exc
    return Request()


def load_credentials(
    paths: GmailPaths,
    *,
    credentials_factory: Callable[[dict[str, Any]], Any] | None = None,
    request_factory: Callable[[], Any] | None = None,
) -> Any:
    """Credencial pronta para uso, com refresh explicito quando expirada.

    Os fabricantes sao injetaveis para que o refresh — e a recusa dele — possam
    ser testados sem rede e sem biblioteca.
    """
    document = load_token_document(paths.token)
    factory = credentials_factory or _google_credentials_factory
    credentials = factory(document)
    if getattr(credentials, "expired", False) and getattr(credentials, "refresh_token", None):
        request = (request_factory or _google_request_factory)()
        try:
            credentials.refresh(request)
        except Exception as exc:  # a biblioteca levanta tipos variados
            raise GmailAuthError(
                "refresh token was refused; reauthorize with `integrations gmail authorize`"
            ) from exc
        _write_private(paths.token, credentials.to_json())
    return credentials


def credentials_summary(paths: GmailPaths) -> dict[str, Any]:
    """Estado da credencial, sem nenhum segredo: o que o `status` pode imprimir."""
    summary: dict[str, Any] = {
        "directory": str(paths.directory),
        "client_secret_present": paths.client_secret.is_file(),
        "token_present": paths.token.is_file(),
        "scope": GMAIL_READONLY_SCOPE,
        "authorized": False,
        "detail": "",
    }
    if not paths.client_secret.is_file():
        summary["detail"] = "client_secret.json missing: create the OAuth client and run authorize"
        return summary
    if not paths.token.is_file():
        summary["detail"] = "not authorized yet: run `integrations gmail authorize`"
        return summary
    try:
        document = load_token_document(paths.token)
    except (GmailConfigError, GmailAuthError) as exc:
        summary["detail"] = str(exc)
        return summary
    summary["authorized"] = True
    summary["scopes"] = sorted(str(scope) for scope in document.get("scopes", []))
    summary["expiry"] = str(document.get("expiry", ""))
    return summary


def authorize(
    paths: GmailPaths,
    *,
    open_browser: bool = True,
    port: int = 0,
    flow_factory: Callable[[str, list[str]], Any] | None = None,
) -> dict[str, Any]:
    """Estabelece o acesso. NAO confirma candidatura nenhuma.

    Fluxo local: consentimento no browser normal do usuario, callback em
    `localhost`, refresh token salvo com `0600`. Depois disso as execucoes usam o
    refresh token sem pedir login de novo.
    """
    if not paths.client_secret.is_file():
        raise GmailConfigError(
            f"client_secret.json is missing: {paths.client_secret} "
            "(create an OAuth client of type 'Desktop app' in Google Cloud)"
        )
    flow = (flow_factory or _google_flow_factory)(str(paths.client_secret), [GMAIL_READONLY_SCOPE])
    credentials = flow.run_local_server(port=port, open_browser=open_browser)
    granted = {str(scope) for scope in getattr(credentials, "scopes", []) or [GMAIL_READONLY_SCOPE]}
    extra = granted - ALLOWED_SCOPES
    if extra:
        # Nao se salva uma credencial mais ampla do que o necessario: apagar o
        # token e a resposta segura, porque o consentimento foi outro.
        raise GmailAuthError(f"granted scopes beyond readonly: {sorted(extra)}")
    _write_private(paths.token, credentials.to_json())
    return {
        "token_path": str(paths.token),
        "scopes": sorted(granted),
        "authorized": True,
    }


def _google_flow_factory(client_secret: str, scopes: list[str]) -> Any:
    try:  # pragma: no cover - depende da biblioteca opcional
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise GmailConfigError(
            "google-auth-oauthlib is not installed; "
            "install the optional group: pip install 'google-api-python-client' 'google-auth-oauthlib'"
        ) from exc
    try:  # pragma: no cover - depende da biblioteca opcional
        return InstalledAppFlow.from_client_secrets_file(client_secret, scopes)
    except (ValueError, KeyError) as exc:  # pragma: no cover
        raise GmailConfigError(
            "client_secret.json is not a usable OAuth client document "
            "(it must be a 'Desktop app' client downloaded from Google Cloud)"
        ) from exc
