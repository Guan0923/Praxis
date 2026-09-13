from __future__ import annotations

import base64
import json
import os
import socket
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.sandbox.native_windows.network_audit import network_denial
from backend.sandbox.runtime.proxy import RunCommandProxy
from tests.test_permission_audit import security_event


def network_event(*, pid=123, filter_id=99, record=12):
    event = ET.fromstring(security_event(event_id=5157, record=record))
    data = event.find("{*}EventData")
    for name, value in {"ProcessID": str(pid), "FilterRTID": str(filter_id)}.items():
        ET.SubElement(data, "{http://schemas.microsoft.com/win/2004/08/events/event}Data", Name=name).text = value
    return ET.tostring(event, encoding="unicode")


@pytest.mark.parametrize("change", [{"pid": 456}, {"filter_id": 100}, {"record": 9}, {"record": 21}])
def test_network_audit_rejects_other_process_filter_and_pid_lifetime(change):
    assert (
        network_denial(
            network_event(**change), process_id=123, after_record=10, before_record=20, filter_ids=frozenset({99})
        )
        is None
    )


def test_network_audit_uses_structured_filter_and_process_fields():
    event = network_denial(
        network_event(), process_id=123, after_record=10, before_record=20, filter_ids=frozenset({99})
    )
    assert event["source"] == "windows_wfp"


def request_proxy(proxy, credential, target):
    client, server = socket.socketpair()
    worker = threading.Thread(target=proxy._handle, args=(server,))
    worker.start()
    auth = base64.b64encode(f"{credential.username}:{credential.password}".encode()).decode()
    try:
        client.settimeout(5)
        client.sendall((f"GET {target} HTTP/1.1\r\nHost: test\r\nProxy-Authorization: Basic {auth}\r\n\r\n").encode())
        result = b""
        while data := client.recv(4096):
            result += data
        return result
    finally:
        client.close()
        worker.join(timeout=5)


def test_proxy_denials_are_bound_to_authenticated_job():
    proxy = RunCommandProxy(17831)
    first = proxy.issue("first", (), ttl_seconds=30)
    proxy.issue("second", (), ttl_seconds=30)
    assert b"403" in request_proxy(proxy, first, "http://blocked.test/")
    assert proxy.revoke_job("second") == ()
    records = proxy.revoke_job("first")
    assert len(records) == 1
    assert records[0] == {
        "source": "sandbox_proxy",
        "reason": "target_not_allowed",
        "job_id": "first",
        "host": "blocked.test",
        "port": 80,
    }
    assert b"407" in request_proxy(proxy, first, "http://blocked.test/")
    assert proxy.revoke_job("first") == ()


class Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"NETWORK-OK")

    def log_message(self, *_args):
        pass


@pytest.fixture
def local_http():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_upstream_http_403_does_not_create_proxy_policy_denial(local_http, monkeypatch):
    from backend.sandbox import NetworkRule

    monkeypatch.setattr(Handler, "status", 403)
    proxy = RunCommandProxy(17831)
    credential = proxy.issue("allowed", (NetworkRule("127.0.0.1"),), ttl_seconds=30)
    assert b"403" in request_proxy(proxy, credential, f"http://127.0.0.1:{local_http}/")
    assert proxy.revoke_job("allowed") == ()


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("PRAXIS_NATIVE_ESCALATION_TEST") != "1",
    reason="requires installed Windows Broker and explicit opt-in",
)
@pytest.mark.parametrize("kind", ["wfp", "proxy"])
@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("child", [False, True])
def test_native_network_denial_approval(tmp_path, local_http, monkeypatch, kind, allow, child):
    from backend.jobs import CommandError
    from backend.sandbox import SandboxLauncher
    from backend.sandbox.control.broker import WindowsBrokerClient
    from backend.tools import WorkspaceCommand
    from tests.test_command_escalation import command_context

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = SandboxLauncher(broker=WindowsBrokerClient.from_system(), lease_store_path=tmp_path / "leases.json")
    requests = []
    context = command_context(workspace, launcher, lambda *args: requests.append(args) or allow)
    commands = WorkspaceCommand(workspace, terminal_type="pwsh")
    if kind == "wfp":
        command = (
            "$c = [Net.Sockets.TcpClient]::new(); try { $c.Connect('127.0.0.1', "
            + str(local_http)
            + "); 'NETWORK-OK' } catch { exit 1 } finally { $c.Dispose() }"
        )
    else:
        command = (
            "curl.exe --silent --fail --noproxy '' --proxy \"$env:HTTP_PROXY\" http://127.0.0.1:"
            + str(local_http)
            + "/; exit $LASTEXITCODE"
        )
    if child:
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        command = (
            "& (Get-Process -Id $PID).Path -NoProfile -NonInteractive -EncodedCommand "
            + encoded
            + "; exit $LASTEXITCODE"
        )
    try:
        if allow:
            result = json.loads(commands.run_with_context(context, command))
            assert result["escalated"] is True
            assert "NETWORK-OK" in result["output"]
        else:
            with pytest.raises(CommandError) as error:
                commands.run_with_context(context, command)
            assert error.value.tool_output == requests[0][2]
        assert len(requests) == 1
        first = json.loads(requests[0][2])
        assert not first["output"].strip()
        expected = "windows_wfp" if kind == "wfp" else "sandbox_proxy"
        assert any(event.get("source") == expected for event in first["permission_denials"])
    finally:
        commands.close()
