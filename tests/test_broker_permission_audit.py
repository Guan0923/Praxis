from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from threading import Thread
from types import SimpleNamespace

import pytest

from backend.sandbox.broker_service import BrokerCredentialPackage
from backend.sandbox.control.broker import BrokerCommandAudit, BrokerManagedProcess, WindowsBrokerClient
from backend.sandbox.errors import SandboxInitializationError
from backend.sandbox.native_broker_adapter import adapter as native
from backend.sandbox.native_windows.audit_policy import SandboxAuditUnavailable


@pytest.fixture
def audited_adapter(tmp_path, monkeypatch):
    events = []

    class Handle:
        def Close(self):
            events.append("token-close")

    class Audit:
        def bind_process(self, pid, logon_sid):
            events.append(("bind", pid, logon_sid))

        def collect(self):
            assert "process-close" in events and "desktop-close" in events
            events.append("collect")
            return ({"event_id": 4656, "process_id": 123, "logon_id": 99},)

    class Auditor:
        def prepare(self):
            events.append("prepare")

        def start(self, account_sid):
            events.append(("checkpoint", account_sid))
            return Audit()

    class Tokens:
        def reserve(self, account, mode):
            events.append("reserve-token")
            return SimpleNamespace(
                token=Handle(),
                account_sid=account.sid,
                logon_sid="logon",
                workspace_cap_sid="workspace-cap",
                temp_cap_sid="temp-cap",
            )

    class Desktop:
        startup_name = "desktop"

        def close(self):
            events.append("desktop-close")

    class Job:
        def __init__(self, *_args):
            pass

        def terminate(self):
            events.append("terminate")

    class Process:
        pid = 123
        returncode = None

        @classmethod
        def launch(cls, *_args, before_resume, logon_sid, **_kwargs):
            events.append("create-suspended")
            before_resume(cls.pid, logon_sid)
            events.append("resume")
            return cls()

        def close(self):
            events.append("process-close")
            self.returncode = 1

    class Aggregate:
        def require_permit(self, *_args):
            pass

        def register(self, *_args):
            pass

        def release(self, *_args):
            events.append("permit-release")

        def close(self):
            pass

    package = BrokerCredentialPackage(
        "generation", "offline", "sid-offline", "unused", "online", "sid-online", "unused"
    )
    adapter = native.WindowsNativeBrokerAdapter(
        credentials=package,
        service_sid="service",
        token_factory=Tokens(),
        permission_auditor=Auditor(),
        desktop_factory=lambda *_args: Desktop(),
    )
    adapter.aggregate.close()
    adapter.aggregate = Aggregate()
    monkeypatch.setattr(native, "WindowsJobObject", Job)
    monkeypatch.setattr(native, "_NativeWindowsProcess", Process)
    policy = {
        "job_id": "job",
        "file_mode": "read_only",
        "network_mode": "no_network",
        "workspaces": [str(tmp_path)],
        "cwd": str(tmp_path),
        "temp_dir": str(tmp_path),
    }
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request = {"policy": policy, "policy_hash": digest, "backend_instance_id": "backend", "user_id": "local"}
    try:
        yield adapter, events, request, tmp_path
    finally:
        adapter.close()


def launch_reserved(fixture):
    adapter, events, request, path = fixture
    reserved = adapter.reserve(request)
    launched = adapter.launch(
        {
            **request,
            "reservation_id": reserved["reservation_id"],
            "capability_digest": reserved["capability_digest"],
            "argv": ["cmd.exe"],
            "cwd": str(path),
            "environment": {"TEMP": str(path), "TMP": str(path)},
        }
    )
    return {"process_id": launched["process_id"], "backend_instance_id": "backend", "job_id": "job", "user_id": "local"}


def test_broker_owns_preparation_binding_and_collection(audited_adapter):
    adapter, events, _, _ = audited_adapter
    control = launch_reserved(audited_adapter)
    assert events.index("prepare") < events.index("reserve-token")
    assert events.index(("checkpoint", "sid-offline")) < events.index("create-suspended")
    assert events.index(("bind", 123, "logon")) < events.index("resume")
    result = adapter.control("process_audit", control)
    assert result["job_id"] == "job" and result["backend_instance_id"] == "backend"
    assert result["denials"][0]["event_id"] == 4656
    assert adapter.control("process_audit", control) == result
    assert events.count("collect") == 1
    assert adapter.release(control)


@pytest.mark.parametrize("field", ["backend_instance_id", "user_id", "job_id", "process_id"])
def test_broker_rejects_cross_owner_audit(audited_adapter, field):
    adapter, events, _, _ = audited_adapter
    control = launch_reserved(audited_adapter)
    with pytest.raises(SandboxInitializationError):
        adapter.control("process_audit", {**control, field: "other"})
    assert "collect" not in events and "process-close" not in events


def test_broker_audit_failure_is_not_empty_evidence(audited_adapter):
    adapter, _, _, _ = audited_adapter
    control = launch_reserved(audited_adapter)
    lease = adapter._processes[control["process_id"]]

    def fail():
        raise SandboxAuditUnavailable("Security log unavailable")

    lease.command_audit.collect = fail
    with pytest.raises(SandboxAuditUnavailable, match="Security log unavailable"):
        adapter.control("process_audit", control)
    assert lease.audit_result is None
    assert adapter.release(control)


def test_prepare_failure_never_creates_token(audited_adapter):
    adapter, events, request, _ = audited_adapter

    def fail():
        raise SandboxAuditUnavailable("Privilege missing")

    adapter._permission_auditor.prepare = fail
    with pytest.raises(SandboxAuditUnavailable, match="Privilege missing"):
        adapter.reserve(request)
    assert "reserve-token" not in events


@pytest.mark.parametrize("field", [None, "process_id", "pid", "backend_instance_id", "job_id", "user_id", "denials"])
def test_backend_accepts_only_bound_broker_evidence(field):
    client = WindowsBrokerClient(backend_instance_id="backend")
    response = {
        "process_id": "process",
        "pid": 123,
        "backend_instance_id": "backend",
        "job_id": "job",
        "user_id": "local",
        "denials": [{"event_id": 4656}],
    }
    if field is not None:
        response[field] = "wrong"
    calls = []
    client.request = lambda operation, body: calls.append((operation, body)) or response
    process = BrokerManagedProcess(
        client, "process", 123, stdin_enabled=False, stdout_enabled=False, stderr_enabled=False
    )
    audit = BrokerCommandAudit(client, process, job_id="job", user_id="local")
    audit.finish()
    assert calls[0] == (
        "process_audit",
        {"process_id": "process", "backend_instance_id": "backend", "job_id": "job", "user_id": "local"},
    )
    if field is not None:
        with pytest.raises(SandboxAuditUnavailable):
            audit.collect()
    else:
        audit.proxy_denials = ({"event_id": "proxy_denied"},)
        assert audit.collect() == ({"event_id": 4656}, {"event_id": "proxy_denied"})


@pytest.mark.parametrize("fail_bind", [False, True])
def test_native_process_binds_audit_before_resume_and_closes_on_failure(monkeypatch, fail_bind):
    from backend.sandbox.native_broker_adapter import process as implementation

    events = []
    closed = []
    next_handle = iter(range(1, 7))
    modules = {
        "pipe": SimpleNamespace(CreatePipe=lambda *_args: (next(next_handle), next(next_handle))),
        "api": SimpleNamespace(SetHandleInformation=lambda *_args: None, CloseHandle=closed.append),
        "con": SimpleNamespace(
            HANDLE_FLAG_INHERIT=1, STARTF_USESTDHANDLES=2, CREATE_SUSPENDED=4, CREATE_UNICODE_ENVIRONMENT=0x400
        ),
        "process": SimpleNamespace(
            STARTUPINFO=SimpleNamespace,
            CreateProcessAsUser=lambda *_args: (7, 8, 123, 456),
            ResumeThread=lambda _handle: events.append("resume"),
        ),
    }
    modules["process"].STARTUPINFO = lambda: SimpleNamespace(dwFlags=0)
    monkeypatch.setattr(implementation, "_modules", lambda: modules)
    monkeypatch.setattr(implementation, "_object_security_attributes", lambda *_args: SimpleNamespace())
    job = SimpleNamespace(
        assign=lambda _handle: events.append("assign"),
        terminate=lambda: events.append("terminate"),
        close=lambda: events.append("job-close"),
    )

    def bind(pid, sid):
        events.append(("bind", pid, sid))
        if fail_bind:
            raise SandboxAuditUnavailable("identity mismatch")

    def launch():
        return implementation._NativeWindowsProcess.launch(
            object(),
            ["cmd.exe"],
            "C:\\workspace",
            {},
            job,
            logon_sid="logon",
            service_sid="service",
            desktop_name="desktop",
            before_resume=bind,
        )

    if fail_bind:
        with pytest.raises(SandboxAuditUnavailable, match="identity mismatch"):
            launch()
        assert "resume" not in events
        assert set(closed) == set(range(1, 9))
        assert events[-2:] == ["terminate", "job-close"]
    else:
        assert launch().pid == 123
        assert events == ["assign", ("bind", 123, "logon"), "resume"]
        assert set(closed) == {1, 4, 6}


def test_successful_command_does_not_collect_denial_events(audited_adapter):
    adapter, events, _, _ = audited_adapter
    control = launch_reserved(audited_adapter)
    lease = adapter._processes[control["process_id"]]

    def close_success():
        events.append("process-close")
        lease.process.returncode = 0

    lease.process.close = close_success
    assert adapter.control("process_audit", control)["denials"] == []
    assert "collect" not in events


def test_audit_bind_failure_prevents_resume(audited_adapter):
    adapter, events, request, path = audited_adapter
    reserved = adapter.reserve(request)

    def bind(*_args):
        raise SandboxAuditUnavailable("Identity verification failed")

    adapter._permission_auditor.start = lambda _sid: SimpleNamespace(bind_process=bind)
    with pytest.raises(SandboxAuditUnavailable, match="Identity verification failed"):
        adapter.launch(
            {
                **request,
                "reservation_id": reserved["reservation_id"],
                "capability_digest": reserved["capability_digest"],
                "argv": ["cmd.exe"],
                "cwd": str(path),
                "environment": {"TEMP": str(path), "TMP": str(path)},
            }
        )
    assert "resume" not in events and "token-close" in events and "desktop-close" in events


@pytest.mark.skipif(os.name != "nt", reason="Real Windows named-pipe audit transport")
def test_real_pipe_audit_from_separate_unelevated_backend(audited_adapter, tmp_path):
    import win32file
    import win32pipe

    from backend.sandbox.broker_service import BrokerConfiguration, WindowsBrokerService
    from tests.test_broker_pipe_connection import create_pipe

    adapter, _, _, _ = audited_adapter
    control = launch_reserved(audited_adapter)
    name = rf"\\.\pipe\praxis-audit-test-{uuid.uuid4().hex}"
    pipe = create_pipe(name)
    configuration = BrokerConfiguration.create(program_data=tmp_path / "broker", pipe_name=name)
    key = b"test-only-audit-key-material-32byt"
    service = WindowsBrokerService(configuration, adapter=adapter, is_windows=True)
    service._key = key
    errors = []

    def serve():
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            payload = bytes(win32file.ReadFile(pipe, 65536)[1])
            win32file.WriteFile(pipe, service.handle(payload))
            win32file.FlushFileBuffers(pipe)
        except Exception as exc:
            errors.append(exc)
        finally:
            pipe.Close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    script = """
import sys
import win32api, win32security
from backend.sandbox.control.broker import WindowsBrokerClient, BrokerManagedProcess, BrokerCommandAudit
privilege = win32security.LookupPrivilegeValue(None, 'SeSecurityPrivilege')
token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
try:
    assert privilege not in dict(win32security.GetTokenInformation(token, win32security.TokenPrivileges))
finally:
    token.Close()
client = WindowsBrokerClient(pipe_name=sys.argv[1], installation_key=bytes.fromhex(sys.argv[2]), is_windows=True, backend_instance_id='backend')
process = BrokerManagedProcess(client, sys.argv[3], 123, stdin_enabled=False, stdout_enabled=False, stderr_enabled=False)
audit = BrokerCommandAudit(client, process, job_id='job', user_id='local')
audit.finish()
assert audit.collect()[0]['event_id'] == 4656
print('ordinary backend received authenticated Broker evidence')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, name, key.hex(), control["process_id"]],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "received authenticated Broker evidence" in result.stdout
        thread.join(5)
        assert not thread.is_alive() and not errors
    finally:
        if thread.is_alive():
            from backend.sandbox.control.pipe_connection import connect_pipe

            try:
                wake = connect_pipe(name, deadline=time.monotonic() + 1)
                try:
                    win32file.WriteFile(wake, b"{}")
                finally:
                    wake.Close()
            except Exception:
                pass
            thread.join(5)
        service._reclaimer.close()


def test_backend_audit_transport_failure_fails_closed():
    def fail(*_args, **_kwargs):
        raise OSError("pipe unavailable")

    process = SimpleNamespace(pid=123, _control=fail)
    audit = BrokerCommandAudit(SimpleNamespace(), process, job_id="job", user_id="local")
    audit.finish()
    with pytest.raises(SandboxAuditUnavailable) as caught:
        audit.collect()
    assert isinstance(caught.value.__cause__, OSError)
