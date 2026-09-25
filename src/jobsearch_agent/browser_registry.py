"""BHOST-001 — registry das sessoes do browser-host persistente.

O browser-host e um PROCESSO separado: ele lanca o Chromium, instala a barreira
de escrita e continua vivo independentemente do `jobsearch-agent`. Este modulo
guarda o ESTADO OPERACIONAL CORRENTE dessas sessoes, no mesmo SQLite das
Applications — o historico vai para `application_events`, como ja acontece com
`applications`/`application_events`.

```text
browser_sessions     estado corrente (STARTING/READY/STOPPING/CLOSED/DEAD/EXPIRED)
application_events   auditoria append-only (browser_session_*)
```

O que NAO entra aqui: URL, cookie, token, query string, conteudo de formulario,
challenge response. A sessao do Chromium carrega isso; o banco nao. O que entra e
o suficiente para REENCONTRAR o browser: host/porta CDP, dono
(`owner_pid` + `owner_instance_id`, porque PID se recicla) e prazos.

Este modulo NAO conhece challenge: ele nao escreve `challenge_handoff_*`. Quem
pergunta ao registry se a sessao continua viva e o `challenge_integration`, e a
decisao de retomar ou abandonar um handoff pertence ao dominio de challenge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import ipaddress
import os
import uuid

from .persistence import Database

#: CIDRs/valores aceitos para o CDP. SO loopback, e so LITERAL: o endpoint CDP da
#: controle total sobre o navegador, e um host configuravel externamente seria uma
#: porta de controle remoto disfarcada de configuracao.
LOOPBACK_LITERALS = frozenset({"127.0.0.1", "::1"})

#: Estados OPERACIONAIS. Nada de `ATTACHED`/`HANDOFF` aqui: isso misturaria o
#: lifecycle do browser com o lifecycle da candidatura. O browser pode continuar
#: READY enquanto o agente esta morto — e isso e o ponto.
LIVE_STATES = frozenset({"STARTING", "READY", "STOPPING"})

#: Eventos de auditoria desta fronteira. Nomes proprios, e nao `challenge_*`.
SESSION_EVENTS = frozenset(
    {
        "browser_session_started",
        "browser_session_ready",
        "browser_session_attached",
        "browser_session_resumed",
        "browser_session_closed",
        "browser_session_dead",
        "browser_session_expired",
    }
)


class BrowserRegistryError(RuntimeError):
    """Erro de registro de sessao (host invalido, transicao invalida, conflito)."""


class UnsafeCdpHost(BrowserRegistryError):
    """O CDP so pode ser exposto em loopback literal."""


class SessionConflict(BrowserRegistryError):
    """Ja existe sessao viva para esta Application."""


class BrowserSessionState(str, Enum):
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    CLOSED = "CLOSED"
    DEAD = "DEAD"
    EXPIRED = "EXPIRED"

    @property
    def live(self) -> bool:
        return self.value in LIVE_STATES


#: Transicoes permitidas. `CLOSED`/`DEAD`/`EXPIRED` sao finais: uma sessao nova
#: comeca do zero, e nao "volta" da morte.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "STARTING": frozenset({"READY", "STOPPING", "DEAD"}),
    "READY": frozenset({"STOPPING", "DEAD", "EXPIRED"}),
    "STOPPING": frozenset({"CLOSED", "DEAD"}),
    "CLOSED": frozenset(),
    "DEAD": frozenset(),
    "EXPIRED": frozenset(),
}


def assert_loopback(host: str) -> str:
    """So aceita `127.0.0.1` ou `::1`, como literal."""
    candidate = str(host or "").strip()
    if candidate not in LOOPBACK_LITERALS:
        raise UnsafeCdpHost(f"CDP host must be a loopback literal (127.0.0.1 or ::1), got {candidate!r}")
    if not ipaddress.ip_address(candidate).is_loopback:  # pragma: no cover - defensivo
        raise UnsafeCdpHost(f"CDP host is not loopback: {candidate!r}")
    return candidate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True)
class BrowserSessionRecord:
    """A sessao como ela vive no banco. Sem material sensivel, por construcao."""

    session_id: str
    application_id: str
    state: str
    cdp_host: str
    cdp_port: int
    owner_pid: int
    owner_instance_id: str
    created_at: str
    last_seen_at: str
    expires_at: str = ""
    closed_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, object]) -> "BrowserSessionRecord":
        return cls(
            session_id=str(row["session_id"]),
            application_id=str(row["application_id"]),
            state=str(row["state"]),
            cdp_host=str(row["cdp_host"]),
            cdp_port=int(row["cdp_port"]),
            owner_pid=int(row["owner_pid"]),
            owner_instance_id=str(row["owner_instance_id"]),
            created_at=str(row["created_at"]),
            last_seen_at=str(row["last_seen_at"]),
            expires_at=str(row.get("expires_at", "")),
            closed_at=str(row.get("closed_at", "")),
        )

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"

    def describe(self) -> dict[str, object]:
        """Forma segura para log/journal: nada alem do que ja esta na tabela."""
        return {
            "session_id": self.session_id,
            "application_id": self.application_id,
            "state": self.state,
            "cdp_host": self.cdp_host,
            "cdp_port": self.cdp_port,
            "owner_instance_id": self.owner_instance_id,
        }


class BrowserRegistry:
    """Estado corrente das sessoes do browser-host, e as transicoes validas."""

    def __init__(self, database: Database, *, clock: object | None = None) -> None:
        self.database = database
        self._clock = clock or _now

    # -- ciclo de vida ---------------------------------------------------------

    def register(
        self,
        *,
        application_id: str,
        cdp_host: str,
        cdp_port: int,
        owner_pid: int | None = None,
        owner_instance_id: str = "",
        ttl_seconds: int = 0,
        session_id: str = "",
    ) -> BrowserSessionRecord:
        """Registra uma sessao nova em STARTING. Uma viva por Application."""
        host = assert_loopback(cdp_host)
        port = int(cdp_port)
        if not 0 < port <= 65535:
            raise BrowserRegistryError(f"CDP port out of range: {port}")
        existing = self.database.live_browser_session_for_application(application_id)
        if existing is not None:
            raise SessionConflict(
                f"application {application_id} already has a live browser session: {existing['session_id']}"
            )
        now = self._clock()
        record = BrowserSessionRecord(
            session_id=session_id or f"bsess-{uuid.uuid4().hex[:16]}",
            application_id=application_id,
            state=BrowserSessionState.STARTING.value,
            cdp_host=host,
            cdp_port=port,
            owner_pid=int(owner_pid if owner_pid is not None else os.getpid()),
            owner_instance_id=owner_instance_id or uuid.uuid4().hex,
            created_at=_iso(now),
            last_seen_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)) if ttl_seconds > 0 else "",
        )
        self.database.save_browser_session(record.__dict__)
        self._event(record, "browser_session_started")
        return record

    def mark_ready(self, session_id: str) -> BrowserSessionRecord:
        return self._transition(session_id, BrowserSessionState.READY, "browser_session_ready")

    def mark_attached(self, session_id: str) -> BrowserSessionRecord:
        """O agente (re)conectou. Nao muda o estado: marca presenca e audita."""
        record = self._require(session_id)
        updated = self._touch(record)
        self._event(updated, "browser_session_attached")
        return updated

    def mark_resumed(self, session_id: str) -> BrowserSessionRecord:
        """O agente voltou e a sessao estava viva: o browser sobreviveu."""
        record = self._require(session_id)
        if not record.live:
            raise BrowserRegistryError(f"session {session_id} is not live: {record.state}")
        updated = self._touch(record)
        self._event(updated, "browser_session_resumed")
        return updated

    def close(self, session_id: str) -> BrowserSessionRecord:
        record = self._require(session_id)
        if record.state in {BrowserSessionState.READY.value, BrowserSessionState.STARTING.value}:
            # Uma sessao que nunca chegou a READY tambem precisa poder ser
            # encerrada: parar e sempre permitido, "voltar da morte" nao.
            record = self._transition(session_id, BrowserSessionState.STOPPING, "")
        return self._transition(record.session_id, BrowserSessionState.CLOSED, "browser_session_closed")

    def mark_dead(self, session_id: str, *, reason: str = "") -> BrowserSessionRecord:
        return self._transition(session_id, BrowserSessionState.DEAD, "browser_session_dead", notes=reason)

    def expire(self, session_id: str) -> BrowserSessionRecord:
        return self._transition(session_id, BrowserSessionState.EXPIRED, "browser_session_expired")

    # -- consultas -------------------------------------------------------------

    def get(self, session_id: str) -> BrowserSessionRecord | None:
        row = self.database.get_browser_session(session_id)
        return BrowserSessionRecord.from_row(row) if row else None

    def live_for(self, application_id: str) -> BrowserSessionRecord | None:
        row = self.database.live_browser_session_for_application(application_id)
        return BrowserSessionRecord.from_row(row) if row else None

    def is_owner_alive(self, session_id: str) -> bool:
        """O processo dono ainda existe?

        PID sozinho nao identifica instancia (PID se recicla): quem decide se a
        sessao e a MESMA e o `owner_instance_id`, que o browser-host gera no
        nascimento e o agente confere ao reconectar.
        """
        record = self.get(session_id)
        if record is None:
            return False
        try:
            os.kill(record.owner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - processo de outro usuario
            return True
        return True

    def can_resume(self, session_id: str) -> bool:
        """Sessao reconectavel: viva no banco, dentro do prazo e pagina utilizavel.

        "Dentro do prazo" e do banco; "pagina utilizavel" quem confirma e o
        agente, tentando o CDP. O registry nao promete o que nao pode verificar.
        """
        record = self.get(session_id)
        if record is None or not record.live:
            return False
        if record.expires_at and record.expires_at <= _iso(self._clock()):
            return False
        return True

    # -- internos --------------------------------------------------------------

    def _require(self, session_id: str) -> BrowserSessionRecord:
        record = self.get(session_id)
        if record is None:
            raise BrowserRegistryError(f"browser session not found: {session_id}")
        return record

    def _transition(self, session_id: str, target: BrowserSessionState, event: str, *, notes: str = "") -> BrowserSessionRecord:
        record = self._require(session_id)
        if target.value not in _TRANSITIONS.get(record.state, frozenset()):
            raise BrowserRegistryError(f"invalid browser session transition: {record.state} -> {target.value}")
        fields: dict[str, object] = {"state": target.value, "last_seen_at": _iso(self._clock())}
        if target in {BrowserSessionState.CLOSED, BrowserSessionState.DEAD, BrowserSessionState.EXPIRED}:
            fields["closed_at"] = fields["last_seen_at"]
        row = self.database.update_browser_session(session_id, **fields)
        updated = BrowserSessionRecord.from_row(row) if row else record
        if event:
            self._event(updated, event, notes=notes)
        return updated

    def _touch(self, record: BrowserSessionRecord) -> BrowserSessionRecord:
        row = self.database.update_browser_session(record.session_id, last_seen_at=_iso(self._clock()))
        return BrowserSessionRecord.from_row(row) if row else record

    def _event(self, record: BrowserSessionRecord, event: str, *, notes: str = "") -> None:
        if event not in SESSION_EVENTS:
            raise BrowserRegistryError(f"unsupported browser session event: {event}")
        payload: dict[str, object] = {**record.describe(), "cdp_host": record.cdp_host, "cdp_port": record.cdp_port}
        if notes:
            payload["notes"] = notes
        self.database.append_application_event(record.application_id, event, payload)


__all__ = [
    "BrowserRegistry",
    "BrowserRegistryError",
    "BrowserSessionRecord",
    "BrowserSessionState",
    "LIVE_STATES",
    "LOOPBACK_LITERALS",
    "SESSION_EVENTS",
    "SessionConflict",
    "UnsafeCdpHost",
    "assert_loopback",
]
