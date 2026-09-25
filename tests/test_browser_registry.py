"""BHOST-001 — registry das sessoes do browser-host persistente.

O que estes testes fixam, e por que cada um importa para producao:

  CDP so em loopback LITERAL      o endpoint da controle total do navegador
  uma sessao viva por Application garantida pelo BANCO (indice parcial)
  estados operacionais            o browser pode estar READY com o agente morto
  dono inequivoco                 owner_pid + owner_instance_id (PID se recicla)
  sem material sensivel           a coluna nao existe, entao nao vaza por descuido
  eventos com nome proprio        `browser_session_*`, nunca `challenge_*`
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobsearch_agent.application import ApplicationService
from jobsearch_agent.browser_registry import (
    BrowserRegistry,
    BrowserRegistryError,
    BrowserSessionState,
    SessionConflict,
    UnsafeCdpHost,
    assert_loopback,
)
from jobsearch_agent.models import Job
from jobsearch_agent.persistence import Database

SESSION_COLUMNS = {
    "session_id",
    "application_id",
    "state",
    "cdp_host",
    "cdp_port",
    "owner_pid",
    "owner_instance_id",
    "created_at",
    "last_seen_at",
    "expires_at",
    "closed_at",
}


def _application(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "bhost.db")
    job = Job(id="job-bhost", source="greenhouse", external_id="1", company="Acme", title="Engineer", description="x")
    database.save_job(job, "greenhouse:1", {})
    return database, ApplicationService(database).create_for_job(job.id).id


def _registry(database: Database) -> BrowserRegistry:
    return BrowserRegistry(database)


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.168.0.10", "10.0.0.1", "", "example.test"])
def test_the_cdp_host_must_be_a_loopback_literal(host: str):
    with pytest.raises(UnsafeCdpHost):
        assert_loopback(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_literals_are_accepted(host: str):
    assert assert_loopback(host) == host


def test_registering_starts_a_session_and_audits_it(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)

    session = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222, owner_pid=4242)

    assert session.state == BrowserSessionState.STARTING.value
    assert session.live is True
    assert session.owner_instance_id, "o dono precisa de identidade propria, nao so PID"
    assert registry.live_for(application_id) == session
    kinds = [event.event for event in database.list_application_events(application_id)]
    assert kinds == ["browser_session_started"]


def test_a_second_live_session_for_the_same_application_is_refused_by_the_database(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)

    with pytest.raises(SessionConflict):
        registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9223)

    assert len(database.list_browser_sessions(application_id)) == 1


def test_closing_frees_the_slot_for_a_new_session(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    first = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)

    closed = registry.close(first.session_id)

    assert closed.state == BrowserSessionState.CLOSED.value
    assert closed.closed_at
    assert registry.live_for(application_id) is None
    second = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9224)
    assert second.session_id != first.session_id


def test_the_state_machine_refuses_to_resurrect_a_dead_session(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    session = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)

    dead = registry.mark_dead(session.session_id, reason="browser process exited")
    assert dead.state == BrowserSessionState.DEAD.value

    for attempt in (registry.mark_ready, registry.mark_resumed, registry.expire):
        with pytest.raises(BrowserRegistryError, match="invalid browser session transition|not live"):
            attempt(session.session_id)


def test_readiness_and_resume_are_audited_with_the_session_events(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    session = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)
    registry.mark_ready(session.session_id)
    registry.mark_attached(session.session_id)
    registry.mark_resumed(session.session_id)

    kinds = [event.event for event in database.list_application_events(application_id)]
    assert kinds == [
        "browser_session_started",
        "browser_session_ready",
        "browser_session_attached",
        "browser_session_resumed",
    ]
    assert not any(kind.startswith("challenge_") for kind in kinds), "o registry nao fala de challenge"


def test_can_resume_requires_a_live_session_within_its_ttl(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    live = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)
    assert registry.can_resume(live.session_id) is True

    # O TTL e do banco; a pagina utilizavel quem confirma e o agente, tentando o
    # CDP — o registry nao promete o que nao pode verificar.
    database.update_browser_session(live.session_id, expires_at="2000-01-01T00:00:00+00:00")
    assert registry.can_resume(live.session_id) is False

    registry.mark_dead(live.session_id)
    assert registry.can_resume(live.session_id) is False


def test_the_owner_is_identified_by_pid_and_instance(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    session = registry.register(
        application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222, owner_pid=1, owner_instance_id="instancia-a"
    )
    assert session.owner_pid == 1 and session.owner_instance_id == "instancia-a"
    # PID 1 existe => vivo; o que decide a IDENTIDADE e o instance id.
    assert registry.is_owner_alive(session.session_id) is True


def test_the_registry_stores_no_page_material(tmp_path: Path):
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    session = registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)
    registry.mark_ready(session.session_id)

    columns = {row[1] for row in database.connection.execute("PRAGMA table_info(browser_sessions)")}
    assert columns == SESSION_COLUMNS, "coluna nova exige decisao explicita (nada de URL/cookie/token)"
    raw = str(database.list_browser_sessions(application_id))
    for forbidden in ("http://", "cookie", "token", "sitekey", "response="):
        assert forbidden not in raw
    assert session.describe()["cdp_host"] == "127.0.0.1"


def test_the_live_slot_is_a_database_invariant_not_a_convention(tmp_path: Path):
    """Escrita direta que fura a invariante tem de ser recusada pelo SQLite."""
    database, application_id = _application(tmp_path)
    registry = _registry(database)
    registry.register(application_id=application_id, cdp_host="127.0.0.1", cdp_port=9222)

    with pytest.raises(Exception) as error:
        database.save_browser_session(
            {
                "session_id": "bsess-fora-do-registry",
                "application_id": application_id,
                "state": "READY",
                "cdp_host": "127.0.0.1",
                "cdp_port": 9999,
                "owner_pid": 1,
                "owner_instance_id": "x",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_seen_at": "2026-01-01T00:00:00+00:00",
            }
        )
    assert "UNIQUE" in str(error.value).upper()
