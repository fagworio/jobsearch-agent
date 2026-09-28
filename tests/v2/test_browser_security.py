from __future__ import annotations

import sys
import json
import os
import socket
import threading

import pytest

from job_agent_v2.browser.client import NativeMessagingClient
from job_agent_v2.browser.native_host import NativeHostServer, read_frame, write_frame
from job_agent_v2.browser.protocol import Command, Request
from job_agent_v2.browser.protocol import Response
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


def test_backend_socket_bridges_a_request_to_the_extension(tmp_path):
    chrome_read, chrome_write = os.pipe()
    chrome_input = os.fdopen(chrome_read, "rb", buffering=0)
    chrome_output = os.fdopen(chrome_write, "wb", buffering=0)
    extension_read, extension_write = os.pipe()
    extension_input = os.fdopen(extension_read, "rb", buffering=0)
    extension_output = os.fdopen(extension_write, "wb", buffering=0)
    socket_path = tmp_path / "native.sock"
    server = NativeHostServer(
        chrome_input,
        extension_output,
        lambda request: Response.success(request.request_id, {"type": "PONG"}),
        socket_path=socket_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = None
    backend_input = None
    backend_output = None
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            threading.Event().wait(0.01)
        backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        backend.connect(str(socket_path))
        backend_input = backend.makefile("rb", buffering=0)
        backend_output = backend.makefile("wb", buffering=0)
        write_frame(backend_output, Request.create("bridge-1", Command.PING).to_json())
        forwarded = json.loads(read_frame(extension_input) or b"{}")
        assert forwarded["type"] == "PING"
        write_frame(chrome_output, Response.success("bridge-1", {"type": "PONG"}).to_json())
        assert json.loads(read_frame(backend_input) or b"{}")["result"] == {"type": "PONG"}
    finally:
        if backend_input is not None:
            backend_input.close()
        if backend_output is not None:
            backend_output.close()
        if backend is not None:
            backend.close()
        chrome_output.close()
        extension_input.close()
        chrome_input.close()
        extension_output.close()
        thread.join(timeout=2)
