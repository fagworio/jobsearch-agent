from __future__ import annotations

from io import BytesIO
import json

import pytest

from job_agent_v2.browser.native_host import NativeHostServer, ping_handler, read_frame, write_frame
from job_agent_v2.browser.protocol import Command, ProtocolError, Request, Response


def test_request_contract_round_trips_without_open_command_surface():
    request = Request.create("req-1", Command.PING, {"client": "test"})
    assert Request.from_object(json.loads(request.to_json())) == request
    with pytest.raises(ProtocolError, match="unsupported command"):
        Request.from_object({"version": 1, "request_id": "req-2", "type": "eval_js", "payload": {}})


def test_response_contract_requires_error_for_failure():
    response = Response.success("req-1", {"type": "PONG"})
    assert Response.from_object(json.loads(response.to_json())) == response
    with pytest.raises(ProtocolError, match="failed response requires error"):
        Response.from_object({"version": 1, "request_id": "req-1", "ok": False})


def test_native_messaging_frame_round_trip():
    stream = BytesIO()
    write_frame(stream, b'{"version":1}')
    stream.seek(0)
    assert read_frame(stream) == b'{"version":1}'


def test_native_host_dispatches_ping_without_domain_logic():
    incoming = BytesIO()
    write_frame(incoming, Request.create("req-7", Command.PING).to_json())
    incoming.seek(0)
    outgoing = BytesIO()
    server = NativeHostServer(incoming, outgoing, lambda request: Response.success(request.request_id, {"type": "PONG"}))
    assert server.serve_once() is True
    outgoing.seek(0)
    assert json.loads(read_frame(outgoing) or b"") == {
        "version": 1,
        "request_id": "req-7",
        "ok": True,
        "result": {"type": "PONG"},
    }


def test_native_host_handler_exposes_only_handshake_commands():
    assert ping_handler(Request.create("hello", Command.HELLO)).result == {
        "name": "job-agent-v2",
        "protocol_version": 1,
    }
    denied = ping_handler(Request.create("submit", Command.REQUEST_SUBMIT))
    assert not denied.ok
