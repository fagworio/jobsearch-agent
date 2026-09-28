from __future__ import annotations

import sys

import pytest

from job_agent_v2.browser.client import NativeMessagingClient
from job_agent_v2.browser.protocol import Command, Request
from job_agent_v2.browser.security import SecurityError, validate_request


def test_security_rejects_arbitrary_execution_payloads():
    with pytest.raises(SecurityError):
        validate_request(Request.create("r1", Command.PING, {"eval_js": "document.body"}))


def test_native_messaging_client_round_trips_ping():
    client = NativeMessagingClient((sys.executable, "-m", "job_agent_v2.browser.native_host"))
    try:
        response = client.ping()
        assert response.ok is True
        assert response.result == {"type": "PONG"}
    finally:
        client.close()
