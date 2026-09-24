"""`GmailEmailSource`: o porto de caixa implementado sobre a Gmail API (JSA-CONF-006B/C).

O observador nao sabe que este modulo existe. Ele recebe `EmailMessage` —
referencia opaca, instante, remetente, assunto e corpo — e o corpo desaparece
assim que o matching termina.

**Consulta incremental.** A busca do Gmail (`after:`) apenas reduz o universo; a
decisao temporal e do PROPRIO sistema: o `internalDate` da mensagem (epoch em ms,
autoritativo) tem de ser `>= since`. A sintaxe de busca do provedor nao decide a
janela — se ela mudar de semantica, a janela continua a mesma.

**Falha nao e ausencia.** 401, 5xx, timeout ou refresh recusado levantam
`ConfirmationSourceUnavailable`. Um erro da API nunca pode virar "nao houve
submissao".
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from ...confirmation import ConfirmationSourceUnavailable, EmailMessage

#: Teto de mensagens por reconciliacao. A janela ja e pequena; o teto existe
#: para uma caixa patologica nao transformar a reconciliacao numa varredura.
DEFAULT_MAX_MESSAGES = 50

#: Teto do corpo mantido em memoria. A frase de recebimento aparece no inicio da
#: mensagem; nada justifica carregar um anexo inteiro em texto.
MAX_BODY_CHARS = 20000

_SENDER_HEADER = "from"
_SUBJECT_HEADER = "subject"
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


class GmailApi(Protocol):
    """O que a fonte precisa da API, em dois metodos. Implementacao real abaixo."""

    def list_message_ids(self, *, after: datetime, limit: int) -> list[str]: ...

    def fetch_message(self, message_id: str) -> dict[str, Any]: ...


def _plain_text(payload: dict[str, Any]) -> str:
    """Texto da mensagem: `text/plain` quando existe, senao HTML sem tags.

    Alguns ATS mandam confirmacao so em HTML. Sem o fallback, a mensagem certa
    ficaria invisivel para o observador.
    """
    plain = _collect(payload, "text/plain")
    if plain:
        return plain[:MAX_BODY_CHARS]
    html = _collect(payload, "text/html")
    if html:
        return _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", html)).strip()[:MAX_BODY_CHARS]
    return ""


def _collect(part: dict[str, Any], mime_type: str) -> str:
    if not isinstance(part, dict):
        return ""
    chunks: list[str] = []
    if str(part.get("mimeType", "")).casefold() == mime_type:
        chunks.append(_decode(part.get("body") or {}))
    for child in part.get("parts") or []:
        chunks.append(_collect(child, mime_type))
    return "\n".join(chunk for chunk in chunks if chunk)


def _decode(body: dict[str, Any]) -> str:
    data = body.get("data") if isinstance(body, dict) else None
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _header(payload: dict[str, Any], name: str) -> str:
    """Cabecalho do MIME. `payload` e o corpo da mensagem (`message["payload"]`)."""
    for header in payload.get("headers") or []:
        if str(header.get("name", "")).casefold() == name:
            return str(header.get("value", ""))
    return ""


def message_from_payload(message_id: str, payload: dict[str, Any], *, since: datetime) -> EmailMessage | None:
    """Traduz o payload da API. `None` quando a mensagem esta fora da janela.

    A validacao aqui e a SEGUNDA: a primeira foi a query. Uma mensagem que a
    busca trouxe mas cujo `internalDate` antecede `since` nao e evidencia, e um
    payload sem `internalDate` nao pode ser situado no tempo — tambem nao e.
    """
    raw_internal = payload.get("internalDate")
    try:
        internal_ms = int(str(raw_internal))
    except (TypeError, ValueError):
        return None
    received = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
    if received < since:
        return None
    body = payload.get("payload") or {}
    return EmailMessage(
        reference=message_id,
        observed_at=received.isoformat(timespec="seconds"),
        sender=_header(body, _SENDER_HEADER),
        subject=_header(body, _SUBJECT_HEADER),
        body=_plain_text(body),
        thread_id=str(payload.get("threadId", "")),
    )


class GmailEmailSource:
    """Caixa do Gmail. `messages_since` devolve so o contrato generico."""

    provider = "gmail"

    def __init__(self, api: GmailApi, *, max_messages: int = DEFAULT_MAX_MESSAGES):
        if max_messages < 1:
            raise ValueError("max_messages must be positive")
        self.api = api
        self.max_messages = max_messages

    def messages_since(self, since: datetime) -> list[EmailMessage]:
        messages: list[EmailMessage] = []
        seen: set[str] = set()
        for message_id in self.api.list_message_ids(after=since, limit=self.max_messages):
            if message_id in seen:
                # A mesma mensagem duas vezes e a MESMA evidencia: o id de
                # conteudo ja deduplica na persistencia, e aqui nao se gasta uma
                # segunda leitura.
                continue
            seen.add(message_id)
            payload = self.api.fetch_message(message_id)
            message = message_from_payload(message_id, payload, since=since)
            if message is not None:
                messages.append(message)
        return messages


class GmailApiClient:
    """`googleapiclient` por tras de dois metodos, com import TARDIO.

    Nenhuma biblioteca do Google e importada no carregamento deste modulo: o
    nucleo e os testes rodam sem elas instaladas.
    """

    def __init__(self, credentials: Any, *, service: Any = None):
        self._credentials = credentials
        self._service = service

    def _client(self) -> Any:
        if self._service is None:
            try:  # pragma: no cover - depende da biblioteca opcional
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover
                raise ConfirmationSourceUnavailable(
                    "google-api-python-client is not installed"
                ) from exc
            self._service = build("gmail", "v1", credentials=self._credentials, cache_discovery=False)
        return self._service

    def list_message_ids(self, *, after: datetime, limit: int) -> list[str]:
        try:
            response = (
                self._client()
                .users()
                .messages()
                .list(userId="me", q=f"after:{int(after.timestamp())}", maxResults=limit)
                .execute()
            )
        except Exception as exc:
            raise ConfirmationSourceUnavailable(f"gmail list failed: {type(exc).__name__}") from exc
        return [str(item["id"]) for item in (response or {}).get("messages", []) if item.get("id")]

    def fetch_message(self, message_id: str) -> dict[str, Any]:
        try:
            return (
                self._client()
                .users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except Exception as exc:
            raise ConfirmationSourceUnavailable(f"gmail get failed: {type(exc).__name__}") from exc
