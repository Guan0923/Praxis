from types import SimpleNamespace

import pytest

from backend.sandbox.control.broker import BrokerStatus, WindowsBrokerClient
from backend.sandbox.control.recovery import recover


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Controller:
    def __init__(self, state="stopped", healthy=True):
        self.state = state
        self.healthy = healthy
        self.calls = []
        self._installer = self

    def service_state(self):
        return {"state": self.state, "exit_code": 0, "service_exit_code": 0}

    def start(self):
        self.calls.append("start")
        self.state = "running"

    def status(self, **_):
        return BrokerStatus(True, self.healthy, service_state=self.state)

    def _status_command(self, operation):
        self.calls.append(operation)
        return BrokerStatus(True, True, service_state="running")


def run(controller, **kwargs):
    clock = Clock()
    result = recover(controller, clock=clock.now, sleep=clock.sleep, **kwargs)
    return result, clock.value


def test_stopped_only_starts_and_healthy_does_not_repair():
    client = Controller()
    status, elapsed = run(client, before_repair=lambda: pytest.fail("unnecessary cleanup"))
    assert status.healthy and elapsed == 0
    assert client.calls == ["start"]


def test_running_healthy_does_nothing():
    client = Controller("running")
    run(client)
    assert client.calls == []


def test_running_unhealthy_repairs_after_cleanup():
    client = Controller("running", False)
    run(client, before_repair=lambda: client.calls.append("cleanup"))
    assert client.calls == ["cleanup", "repair"]


def test_start_unhealthy_waits_ten_seconds_then_cleans_and_repairs():
    client = Controller(healthy=False)
    _, elapsed = run(client, before_repair=lambda: client.calls.append("cleanup"))
    assert elapsed == 10
    assert client.calls == ["start", "cleanup", "repair"]


def test_missing_installs():
    client = Controller("missing")
    run(client)
    assert client.calls == ["install"]


@pytest.mark.parametrize("stage", ["start", "service_state"])
def test_original_failure_pauses_without_repair(stage):
    client = Controller()
    original = OSError(5, "test access denied")

    def fail():
        raise original

    setattr(client, stage, fail)
    with pytest.raises(OSError) as caught:
        run(client)
    assert caught.value is original
    assert original.broker_recovery_code == (
        "broker_service_state_failed" if stage == "service_state" else "broker_service_start_failed"
    )
    assert "repair" not in client.calls


def test_start_returning_stopped_is_failure_not_repair():
    client = Controller()
    client.start = lambda: client.calls.append("start")
    with pytest.raises(RuntimeError, match="state=stopped"):
        run(client)
    assert client.calls == ["start"]


@pytest.mark.parametrize("state", ["start_pending", "stop_pending"])
def test_pending_timeout_does_not_start_or_repair(state):
    client = Controller(state)
    with pytest.raises(TimeoutError, match="10 seconds") as caught:
        run(client)
    assert caught.value.broker_recovery_code == "broker_service_start_failed"
    assert client.calls == []


def test_pending_start_completes_without_duplicate_start():
    client = Controller("running")
    states = iter(["start_pending", "running"])
    client.service_state = lambda: {"state": next(states), "exit_code": 0, "service_exit_code": 0}
    status, elapsed = run(client)
    assert status.healthy and elapsed == 1
    assert client.calls == []


def test_stopped_status_does_not_connect_to_pipe():
    client = WindowsBrokerClient(
        installer=Controller(),
        is_windows=True,
        transport=lambda _: pytest.fail("stopped service must not connect to a pipe"),
    )
    status = client.status()
    assert status.installed and not status.healthy
    assert status.service_state == "stopped"
    assert status.error_report is None


def test_query_failure_status_keeps_real_exception():
    def fail():
        raise PermissionError(5, "test status denied")

    client = WindowsBrokerClient(installer=SimpleNamespace(service_state=fail), is_windows=True)
    status = client.status()
    assert status.code == "broker_service_state_failed"
    assert status.error_report["type"] == "PermissionError"


def test_real_local_windows_service_query_is_read_only():
    import os

    if os.name != "nt":
        pytest.skip("Windows service manager")
    from backend.sandbox.control.service_state import query_service

    result = query_service("PraxisSandboxBroker")
    assert result["state"] in {"missing", "stopped", "start_pending", "stop_pending", "running", "paused"}
    assert isinstance(result["exit_code"], int)
    assert query_service("PraxisMissingRecoveryTestService")["state"] == "missing"


@pytest.mark.parametrize("missing_binary", [False, True])
def test_real_isolated_windows_service_start_and_failure(tmp_path, missing_binary):
    import ctypes
    import os
    import subprocess
    import time
    import uuid
    from pathlib import Path

    if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("Independent service test needs an elevated Windows process")
    import win32service

    from backend.sandbox.broker_service.installer import WindowsServiceInstaller
    from backend.sandbox.control.service_state import query_service

    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        pytest.skip("Local .NET Framework compiler unavailable")
    name = "PraxisStartProbe" + uuid.uuid4().hex
    executable = tmp_path / "SandboxStartProbe.exe"
    if not missing_binary:
        subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/target:exe",
                "/r:System.ServiceProcess.dll",
                f"/out:{executable}",
                str(Path(__file__).parent / "support/SandboxStartProbe.cs"),
            ],
            check=True,
            capture_output=True,
        )
    production = query_service("PraxisSandboxBroker")
    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CREATE_SERVICE)
    service = None
    try:
        service = win32service.CreateService(
            manager,
            name,
            name,
            win32service.SERVICE_ALL_ACCESS,
            win32service.SERVICE_WIN32_OWN_PROCESS,
            win32service.SERVICE_DEMAND_START,
            win32service.SERVICE_ERROR_NORMAL,
            subprocess.list2cmdline([str(executable), name]),
            None,
            0,
            None,
            None,
            None,
        )
        installer = WindowsServiceInstaller((str(executable),), service_name=name, is_windows=True)
        client = Controller()
        client._installer = installer
        client.status = lambda **_: BrokerStatus(True, query_service(name)["state"] == "running")
        if missing_binary:
            with pytest.raises(Exception) as caught:
                recover(client)
            assert caught.value.winerror == 2
            assert caught.value.broker_recovery_code == "broker_service_start_failed"
        else:
            assert recover(client).healthy
            assert query_service(name)["state"] == "running"
            # A second recovery must not restart the already healthy service.
            assert recover(client).healthy
        assert client.calls == []
        assert query_service("PraxisSandboxBroker") == production
    finally:
        if service is not None:
            if query_service(name)["state"] != "stopped":
                win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                deadline = time.monotonic() + 10
                while query_service(name)["state"] != "stopped" and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert query_service(name)["state"] == "stopped"
            win32service.DeleteService(service)
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(manager)


def test_start_helper_does_not_run_installation_steps(monkeypatch):
    from backend.sandbox import install_helper
    from backend.sandbox.control import service_state

    started = []
    monkeypatch.setattr(service_state, "start_service", started.append)
    for name in (
        "_prepare_service_host",
        "_stop_service_for_repair",
        "_provision_fixed_accounts",
        "_secure_program_data",
        "_persist_sid",
        "_initialize_installation_key",
    ):
        monkeypatch.setattr(install_helper, name, lambda *a, **k: pytest.fail("start executed installation steps"))
    result = install_helper._run_transaction(
        {"operation": "start", "service_name": "test-service", "service_command": ["unused"]}
    )
    assert result == 0 and started == ["test-service"]


def test_health_query_failure_never_falls_through_to_repair():
    client = Controller("running")
    client.status = lambda **_: BrokerStatus(
        True, False, code="broker_service_state_failed", detail="original query failure"
    )
    with pytest.raises(RuntimeError, match="original query failure") as caught:
        run(client)
    assert caught.value.broker_recovery_code == "broker_service_state_failed"
    assert client.calls == []


def test_http_start_failure_preserves_error_and_healthy_start_skips_manifest(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routes import sandbox
    from backend.sandbox.control.maintenance import SandboxMaintenanceGate

    controller = Controller()
    broker = WindowsBrokerClient(installer=controller, is_windows=True)
    monkeypatch.setattr(broker, "status", controller.status)
    app = FastAPI()
    app.include_router(sandbox.router)
    app.state.web = SimpleNamespace(
        sandbox_broker=broker,
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=tmp_path / "manifest.json",
    )
    monkeypatch.setattr(
        sandbox, "_manifest_has_records", lambda _: pytest.fail("healthy start inspected repair manifest")
    )
    with TestClient(app) as client:
        response = client.post("/api/sandbox/repair")
        assert response.status_code == 200
        assert controller.calls == ["start"]
        controller.state = "stopped"

        def fail():
            raise FileNotFoundError(2, "test executable missing")

        controller.start = fail
        response = client.post("/api/sandbox/repair")
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == "broker_service_start_failed"
        assert body["error_report"]["type"] == "FileNotFoundError"
        assert body["error_report"]["errno"] == 2
        assert "fail" in body["error_report"]["traceback"]


def test_stopping_service_settles_before_one_start():
    client = Controller("stop_pending")
    remaining = ["stop_pending", "stopped"]

    def query():
        state = remaining.pop(0) if remaining else client.state
        return {"state": state, "exit_code": 0, "service_exit_code": 0}

    client.service_state = query
    status, elapsed = run(client)
    assert status.healthy and elapsed == 1
    assert client.calls == ["start"]


def test_service_becomes_healthy_during_start_observation():
    client = Controller()
    clock = Clock()
    client.status = lambda **_: BrokerStatus(True, clock.value >= 3, service_state="running")
    assert recover(client, clock=clock.now, sleep=clock.sleep).healthy
    assert clock.value == 3
    assert client.calls == ["start"]


@pytest.mark.parametrize("respond", [True, False])
def test_real_status_pipe_reply_and_timeout(respond):
    import os
    import time
    import uuid
    from threading import Event, Thread

    if os.name != "nt":
        pytest.skip("Windows named pipes")
    import win32file
    import win32pipe

    from backend.sandbox.control.status_request import status_request

    name = "\\\\.\\pipe\\PraxisStatusProbe" + uuid.uuid4().hex
    pipe = win32pipe.CreateNamedPipe(
        name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE,
        1,
        65536,
        65536,
        0,
        None,
    )
    release = Event()
    received = []

    def server():
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
            received.append(win32file.ReadFile(pipe, 4096)[1])
            if respond:
                win32file.WriteFile(pipe, b"reply")
            release.wait(5)
        finally:
            pipe.Close()

    thread = Thread(target=server)
    thread.start()
    try:
        if respond:
            assert status_request(name, b"probe", 1) == b"reply"
        else:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                status_request(name, b"probe", 0.1)
            assert time.monotonic() - started < 2
        assert received == [b"probe"]
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()


def test_missing_controller_preserves_initialization_error_and_pauses():
    client = WindowsBrokerClient(is_windows=True)
    original = ImportError("test service dependency missing")
    client._initialization_error = original
    with pytest.raises(ImportError) as caught:
        client.repair()
    assert caught.value is original
    assert original.broker_recovery_code == "broker_service_state_failed"
