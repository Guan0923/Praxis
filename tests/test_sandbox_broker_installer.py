from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import Request

from backend.api.routes.sandbox import install as install_broker
from backend.api.routes.sandbox import repair as repair_broker
from backend.api.routes.sandbox import status as status_broker
from backend.sandbox import (
    BrokerStatusFailureCode,
    SandboxMaintenanceBusy,
    SandboxMaintenanceGate,
    WindowsBrokerClient,
)
from backend.sandbox.broker_service import (
    BrokerCredentialPackage,
    WindowsServiceInstaller,
    build_ready_marker,
)
from backend.sandbox.broker_service.installer import (
    _absolute_windows_path,
    _elevated_helper_argv,
    _normalized_windows_path,
    _service_class_matches,
    _service_command_check,
    _write_python_service_path,
)
from backend.sandbox.errors import BrokerInstallationError, BrokerInstallFailureCode
from backend.sandbox.install_helper import (
    EXIT_SERVICE_STOP_FAILED,
    _icacls_sid,
    _persist_sid,
    _runtime_acl_grants,
    _secure_program_data,
    _stop_service_for_repair,
    _TransactionFailure,
    run_transaction,
)
from backend.sandbox.installation import access_policy
from backend.sandbox.installation.access_policy import _service_sid, _source_acl_grants


@pytest.fixture(autouse=True)
def isolate_helper_host_and_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.sandbox.install_helper.installation_lock", lambda _name: nullcontext())
    monkeypatch.setattr("backend.sandbox.install_helper._prepare_service_host", lambda command, _class: command)
    monkeypatch.setattr("backend.sandbox.install_helper._service_exists", lambda _name: True)
    monkeypatch.setattr("backend.sandbox.native_windows.security._set_directory_dacl_direct", lambda *_args: None)


class _Result:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


class _StatusInstaller:
    def __init__(self, *, installed: bool = True, configuration_healthy: bool = True) -> None:
        self.installed = installed
        self.healthy = configuration_healthy

    def service_installed(self) -> bool:
        return self.installed

    def configuration_healthy(self) -> bool:
        return self.healthy


def _status_transport(key: bytes, **overrides: object):
    def transport(payload: bytes) -> bytes:
        request = json.loads(payload)
        response = {
            "nonce": request["nonce"],
            "installed": True,
            "healthy": True,
            "version": "3",
            "generation": "generation-1",
            "proxy_port": 17831,
            "token_model": "capability_sid_v3",
            **overrides,
        }
        response["hmac"] = hmac.new(
            key,
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return json.dumps(response, separators=(",", ":")).encode()

    return transport


def test_broker_status_reports_only_confirmed_missing_service_as_not_installed() -> None:
    status = WindowsBrokerClient(is_windows=True, installer=_StatusInstaller(installed=False)).status()

    assert status.installed is False
    assert status.healthy is False
    assert status.code is BrokerStatusFailureCode.NOT_INSTALLED
    assert status.detail == "Windows Broker is not installed"


@pytest.mark.parametrize("old_key", [b"o" * 32, b"n" * 32])
def test_repair_runs_even_when_healthy_and_reloads_the_installation_key(old_key: bytes) -> None:
    new_key = b"n" * 32
    calls: list[str] = []

    class Installer:
        def repair(self) -> None:
            calls.append("repair")

        def service_installed(self) -> bool:
            return True

        def configuration_healthy(self) -> bool:
            return True

    class KeyStore:
        def load(self) -> bytes:
            calls.append("load")
            return new_key

    client = WindowsBrokerClient(
        installation_key=old_key,
        transport=_status_transport(new_key),
        is_windows=True,
        installer=Installer(),
        key_store=KeyStore(),
    )

    status = client.repair()

    assert status.healthy is True
    assert calls == ["repair", "load"]


def test_broker_status_preserves_safe_initialization_detail_for_installed_service() -> None:
    status = WindowsBrokerClient(is_windows=True, installer=_StatusInstaller()).status()

    assert status.installed is True
    assert status.healthy is False
    assert status.code is BrokerStatusFailureCode.INSTALLATION_KEY_MISSING
    assert status.detail == "Broker installation key is missing"


def test_broker_status_distinguishes_invalid_service_configuration() -> None:
    status = WindowsBrokerClient(
        is_windows=True,
        installer=_StatusInstaller(configuration_healthy=False),
    ).status()

    assert status.installed is True
    assert status.code is BrokerStatusFailureCode.SERVICE_CONFIGURATION_INVALID
    assert status.detail == "Broker service configuration requires repair"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            r"C:\Program Files\Praxis Project\pythonservice.exe",
            r"\\?\C:\Program Files\Praxis Project\pythonservice.exe",
        ),
        (
            r"C:\Program Files\Praxis Project\pythonservice.exe",
            r"c:/program files/praxis project/pythonservice.exe",
        ),
        (
            r"\\server\share\Praxis Project\pythonservice.exe",
            r"\\?\UNC\SERVER\SHARE\Praxis Project\pythonservice.exe",
        ),
    ],
)
def test_windows_path_normalization_treats_equivalent_spellings_as_equal(left: str, right: str) -> None:
    assert _normalized_windows_path(left) == _normalized_windows_path(right)


def test_absolute_windows_path_removes_extended_prefix() -> None:
    assert _absolute_windows_path(r"\\?\C:\Praxis Project\pythonservice.exe") == (
        r"C:\Praxis Project\pythonservice.exe"
    )


@pytest.mark.skipif(os.name != "nt", reason="uses the Windows command-line parser")
def test_service_command_check_normalizes_only_the_executable_path() -> None:
    expected = (r"C:\Program Files\Praxis Project\pythonservice.exe", "--mode", "broker value")
    equivalent = subprocess.list2cmdline(
        [r"\\?\c:\program files\praxis project\pythonservice.exe", "--mode", "broker value"]
    )

    assert _service_command_check(equivalent, expected)[0] is None
    assert (
        _service_command_check(
            subprocess.list2cmdline([expected[0], "--mode", "other value"]),
            expected,
        )[0]
        == "service_arguments"
    )
    assert (
        _service_command_check(
            subprocess.list2cmdline([r"C:\Program Files\Other\pythonservice.exe", "--mode", "broker value"]),
            expected,
        )[0]
        == "service_executable"
    )


@pytest.mark.skipif(os.name != "nt", reason="uses the Windows command-line parser")
def test_configuration_health_accepts_equivalent_service_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    expected_command = (r"C:\Program Files\Praxis Project\pythonservice.exe", "--mode", "broker value")
    expected_class = r"C:\Praxis Project\backend\src\sandbox_service_bootstrap.PraxisSandboxBrokerService"
    config = [
        16,
        3,
        0,
        subprocess.list2cmdline([r"\\?\c:\program files\praxis project\pythonservice.exe", "--mode", "broker value"]),
        None,
        None,
        None,
        r"NT SERVICE\PraxisSandboxBroker",
    ]
    fake_win32service = types.SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_CONFIG=2,
        SERVICE_CONFIG_SERVICE_SID_INFO=3,
        SERVICE_WIN32_OWN_PROCESS=16,
        SERVICE_DEMAND_START=3,
        SERVICE_SID_TYPE_UNRESTRICTED=1,
        OpenSCManager=lambda *args: object(),
        OpenService=lambda *args: object(),
        QueryServiceConfig=lambda _service: config,
        QueryServiceConfig2=lambda *_args: 1,
        CloseServiceHandle=lambda _handle: None,
    )
    fake_winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        KEY_READ=2,
        REG_SZ=1,
        OpenKey=lambda *_args: RegistryKey(),
        QueryValueEx=lambda *_args: (
            r"\\?\c:/praxis project/backend/src/sandbox_service_bootstrap.PraxisSandboxBrokerService",
            1,
        ),
    )
    monkeypatch.setitem(sys.modules, "win32service", fake_win32service)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    installer = WindowsServiceInstaller(
        expected_command,
        service_class=expected_class,
        is_windows=True,
    )

    assert installer.configuration_healthy() is True


def test_service_class_normalizes_only_the_directory() -> None:
    expected = r"C:\Praxis Project\backend\src\sandbox_service_bootstrap.PraxisSandboxBrokerService"

    assert _service_class_matches(
        r"\\?\c:/praxis project/backend/src/sandbox_service_bootstrap.PraxisSandboxBrokerService",
        expected,
    )
    assert not _service_class_matches(
        r"C:\Praxis Project\backend\src\sandbox_service_bootstrap.OtherService",
        expected,
    )
    assert not _service_class_matches(
        r"C:\Other\backend\src\sandbox_service_bootstrap.PraxisSandboxBrokerService",
        expected,
    )


def test_configuration_diagnostics_are_redacted_limited_and_deduplicated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    installer = WindowsServiceInstaller(("pythonservice.exe",), is_windows=True)
    sensitive_path = rf"C:\api_key=secret-value\{'x' * 400}\pythonservice.exe"
    caplog.set_level(logging.WARNING, logger="backend.sandbox.broker_service.installer")

    installer._configuration_failure("service_executable", actual=sensitive_path)
    installer._configuration_failure("service_executable", actual=sensitive_path)
    installer._configuration_recovered()
    installer._configuration_failure("service_executable", actual=sensitive_path)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert all("secret-value" not in message for message in messages)
    assert all("[REDACTED]" in message for message in messages)
    assert all(len(message) < 400 for message in messages)


def test_configuration_read_failure_logs_step_error_type_and_winerror_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ServiceReadError(Exception):
        winerror = 5

    fake_win32service = types.SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        OpenSCManager=lambda *args: (_ for _ in ()).throw(ServiceReadError("api_key=secret-value")),
    )
    monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32service", fake_win32service)
    installer = WindowsServiceInstaller(("pythonservice.exe",), is_windows=True)
    caplog.set_level(logging.WARNING, logger="backend.sandbox.broker_service.installer")

    with pytest.raises(ServiceReadError):
        installer.configuration_healthy()
    with pytest.raises(ServiceReadError):
        installer.configuration_healthy()

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "field=configuration_read_failed" in messages[0]
    assert "step=open_service_manager" in messages[0]
    assert "error_type=ServiceReadError" in messages[0]
    assert "winerror=5" in messages[0]
    assert "secret-value" not in messages[0]


@pytest.mark.parametrize(
    ("marker", "expected_code", "expected_detail"),
    [
        (None, BrokerStatusFailureCode.READY_MARKER_UNAVAILABLE, None),
        ({"invalid": True}, BrokerStatusFailureCode.READY_MARKER_INVALID, "Broker ready marker is invalid"),
    ],
)
def test_broker_status_distinguishes_ready_marker_failures(
    tmp_path: Path,
    marker: dict[str, object] | None,
    expected_code: BrokerStatusFailureCode,
    expected_detail: str | None,
) -> None:
    ready_path = tmp_path / "ready.json"
    if marker is not None:
        ready_path.write_text(json.dumps(marker), encoding="utf-8")
    status = WindowsBrokerClient(
        installation_key=b"k" * 32,
        transport=_status_transport(b"k" * 32),
        is_windows=True,
        installer=_StatusInstaller(),
        ready_path=ready_path,
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    if expected_detail is None:
        assert status.detail is not None and ready_path.name in status.detail
    else:
        assert status.detail == expected_detail


@pytest.mark.parametrize(
    ("proxy_port", "generation", "expected_code", "expected_detail"),
    [
        (
            17832,
            "generation-1",
            BrokerStatusFailureCode.PROXY_CONFIGURATION_INVALID,
            "Broker proxy port requires repair",
        ),
        (
            17831,
            "generation-2",
            BrokerStatusFailureCode.GENERATION_MISMATCH,
            "Broker generation requires repair",
        ),
    ],
)
def test_broker_status_distinguishes_marker_configuration_mismatches(
    tmp_path: Path,
    proxy_port: int,
    generation: str,
    expected_code: BrokerStatusFailureCode,
    expected_detail: str,
) -> None:
    key = b"k" * 32
    package = BrokerCredentialPackage(
        "generation-1",
        "SandboxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "SandboxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    ready_path = tmp_path / "ready.json"
    ready_path.write_text(json.dumps(build_ready_marker(package, proxy_port)), encoding="utf-8")
    status = WindowsBrokerClient(
        installation_key=key,
        transport=_status_transport(key, generation=generation),
        is_windows=True,
        installer=_StatusInstaller(),
        ready_path=ready_path,
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == expected_detail


@pytest.mark.parametrize(
    ("overrides", "expected_code", "expected_detail"),
    [
        (
            {"version": "1"},
            BrokerStatusFailureCode.PROTOCOL_INCOMPATIBLE,
            "Broker protocol version requires repair",
        ),
        (
            {"token_model": "legacy"},
            BrokerStatusFailureCode.TOKEN_MODEL_INCOMPATIBLE,
            "Broker token model requires repair",
        ),
        (
            {"healthy": False, "detail": "Broker service stopped"},
            BrokerStatusFailureCode.UNHEALTHY,
            "Broker service stopped",
        ),
    ],
)
def test_broker_status_classifies_protocol_and_health_failures(
    overrides: dict[str, object],
    expected_code: BrokerStatusFailureCode,
    expected_detail: str,
) -> None:
    key = b"k" * 32
    status = WindowsBrokerClient(
        installation_key=key,
        transport=_status_transport(key, **overrides),
        is_windows=True,
        installer=_StatusInstaller(),
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == expected_detail


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (OSError(22, "Invalid argument"), BrokerStatusFailureCode.PIPE_UNAVAILABLE),
        (RuntimeError("  exact raw status failure\n"), BrokerStatusFailureCode.STATUS_FAILED),
    ],
)
def test_broker_status_preserves_complete_transport_failure(
    failure: Exception,
    expected_code: BrokerStatusFailureCode,
) -> None:
    def transport(payload: bytes) -> bytes:
        del payload
        raise failure

    status = WindowsBrokerClient(
        installation_key=b"k" * 32,
        transport=transport,
        is_windows=True,
        installer=_StatusInstaller(),
    ).status()

    assert status.installed is True
    assert status.code is expected_code
    assert status.detail == str(failure)


def test_broker_status_api_exposes_code_and_complete_detail() -> None:
    detail = "  line one\nline two: exact diagnostic\n"

    class Broker:
        def status(self):
            return {
                "installed": True,
                "healthy": False,
                "code": BrokerStatusFailureCode.STATUS_FAILED.value,
                "detail": detail,
            }

    app = types.SimpleNamespace(state=types.SimpleNamespace(web=types.SimpleNamespace(sandbox_broker=Broker())))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/sandbox/status",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    assert status_broker(request) == {
        "installed": True,
        "healthy": False,
        "code": "broker_status_failed",
        "detail": detail,
    }


@pytest.mark.parametrize(("winerror", "expected"), [(1060, False), (None, True)])
def test_service_installed_only_returns_false_for_missing_service(
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
    expected: bool,
) -> None:
    class ServiceError(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(code, "service error")
            self.winerror = code

    service_handle = object()
    fake_win32service = types.SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=4,
        OpenSCManager=lambda *args: object(),
        OpenService=(
            (lambda *args: service_handle)
            if winerror is None
            else (lambda *args: (_ for _ in ()).throw(ServiceError(winerror)))
        ),
        CloseServiceHandle=lambda handle: None,
    )
    monkeypatch.setitem(sys.modules, "win32service", fake_win32service)
    installer = WindowsServiceInstaller(("pythonservice.exe",), is_windows=True)

    assert installer.service_installed() is expected


def test_service_installed_preserves_non_missing_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class AccessDenied(Exception):
        winerror = 5

    fake_win32service = types.SimpleNamespace(
        SC_MANAGER_CONNECT=1,
        SERVICE_QUERY_STATUS=4,
        OpenSCManager=lambda *args: object(),
        OpenService=lambda *args: (_ for _ in ()).throw(AccessDenied("denied")),
        CloseServiceHandle=lambda handle: None,
    )
    monkeypatch.setitem(sys.modules, "win32service", fake_win32service)
    installer = WindowsServiceInstaller(("pythonservice.exe",), is_windows=True)

    with pytest.raises(AccessDenied, match="denied"):
        installer.service_installed()


def test_injected_runner_executes_one_local_transaction() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _Result()

    installer = WindowsServiceInstaller(
        ("python.exe", "-m", "backend.sandbox.service_main", "run"),
        runner=runner,
        is_windows=True,
    )
    installer.install()

    assert [call[:2] for call in calls] == [["sc.exe", "create"], ["sc.exe", "sidtype"], ["sc.exe", "start"]]
    assert calls[0][calls[0].index("obj=") + 1] == r"NT SERVICE\PraxisSandboxBroker"


@pytest.mark.skipif(os.name != "nt", reason="Windows service host test")
def test_pywin32_service_host_overwrites_changed_binaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host = tmp_path / "runtime" / "pythonservice.exe"
    host.parent.mkdir()
    host.write_bytes(b"host")
    python_dll = tmp_path / "base" / "python312.dll"
    python_dll.parent.mkdir()
    python_dll.write_bytes(b"python-runtime")
    (host.parent / python_dll.name).write_bytes(b"outdated-bytes")
    pywintypes_dll = tmp_path / "packages" / "pywintypes312.dll"
    pywintypes_dll.parent.mkdir()
    pywintypes_dll.write_bytes(b"pywin32-runtime")
    servicemanager_pyd = tmp_path / "packages" / "servicemanager.pyd"
    servicemanager_pyd.write_bytes(b"service-manager")
    base_prefix = tmp_path / "base runtime" / "python"
    environment_prefix = tmp_path / "environment runtime" / "venv"
    monkeypatch.setattr(sys, "base_prefix", rf"\\?\{base_prefix}")
    monkeypatch.setattr(sys, "prefix", rf"\\?\{environment_prefix}")
    module = types.SimpleNamespace(LocatePythonServiceExe=lambda: rf"\\?\{host}")
    monkeypatch.setitem(sys.modules, "win32serviceutil", module)
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        types.SimpleNamespace(GetModuleFileName=lambda _handle: str(python_dll)),
    )
    monkeypatch.setitem(sys.modules, "pywintypes", types.SimpleNamespace(__file__=str(pywintypes_dll)))
    monkeypatch.setitem(sys.modules, "servicemanager", types.SimpleNamespace(__file__=str(servicemanager_pyd)))
    installer = WindowsServiceInstaller(
        ("placeholder.exe",),
        service_class="sandbox_service_bootstrap.PraxisSandboxBrokerService",
        is_windows=True,
    )

    installer._prepare_service_host()

    assert installer.service_command == (str(host.resolve()),)
    assert (host.parent / python_dll.name).read_bytes() == b"python-runtime"
    assert (host.parent / pywintypes_dll.name).read_bytes() == b"pywin32-runtime"
    assert (host.parent / servicemanager_pyd.name).read_bytes() == b"service-manager"
    path_file = host.parent / f"python{sys.version_info.major}{sys.version_info.minor}._pth"
    assert "\\\\?\\" not in path_file.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime path normalization test")
def test_broker_factory_normalizes_prefixed_runtime_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "prefix", r"\\?\C:\Praxis Project\.venv")
    monkeypatch.setattr(sys, "base_prefix", r"\\?\C:\Runtime\Python312")

    client = WindowsBrokerClient.from_system()

    assert client._installer.service_command == (r"C:\Praxis Project\.venv\pythonservice.exe",)
    assert client._installer.service_runtime_paths == (
        Path(r"C:\Praxis Project\.venv"),
        Path(r"C:\Runtime\Python312"),
    )


def test_python_service_path_lists_only_explicit_import_roots(tmp_path: Path) -> None:
    executable = tmp_path / "venv" / "pythonservice.exe"
    executable.parent.mkdir()
    base = tmp_path / "base"
    environment = tmp_path / "venv"

    path_file = _write_python_service_path(executable, base, environment)

    assert path_file.read_text(encoding="utf-8").splitlines() == [
        ".",
        str((base / f"python{sys.version_info.major}{sys.version_info.minor}.zip").resolve()),
        str((base / "Lib").resolve()),
        str((base / "DLLs").resolve()),
        str((environment / "Lib" / "site-packages").resolve()),
        str((environment / "Lib" / "site-packages" / "win32").resolve()),
        str((environment / "Lib" / "site-packages" / "win32" / "lib").resolve()),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows service ACL test")
def test_injected_runner_uses_prefixed_sid_for_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(Path, "write_text", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: "S-1-5-21-1-2-3-500")

    def runner(command, **kwargs):
        calls.append(list(command))
        return Result()

    installer = WindowsServiceInstaller(
        ("python.exe", "-m", "backend.sandbox.service_main", "run"),
        runner=runner,
        is_windows=True,
        backend_sid_path=Path("C:/ProgramData/Praxis/SandboxBroker/backend.sid"),
        program_data_path=Path("C:/ProgramData/Praxis/SandboxBroker"),
        service_code_path=Path("C:/workspace/praxis/backend/src"),
        service_code_boundary_path=Path("C:/workspace/praxis"),
    )
    installer._run_local_transaction("repair", "S-1-5-21-1-2-3-500")

    program_data_call = next(call for call in calls if call[:2] == ["icacls.exe", str(installer.program_data_path)])
    sid_calls = [call for call in calls if call[:2] == ["icacls.exe", str(installer.backend_sid_path)]]
    key_path = installer.program_data_path / "installation.key.dpapi"
    takeown_call = next(call for call in calls if call[:3] == ["takeown.exe", "/F", str(key_path)])
    key_acl_call = next(call for call in calls if call[:2] == ["icacls.exe", str(key_path)])
    source_call = next(call for call in calls if call[:2] == ["win32-acl", str(installer.service_code_path)])
    service_sid = _service_sid("PraxisSandboxBroker")

    assert "*S-1-5-21-1-2-3-500:(OI)(CI)(M)" in program_data_call
    assert len(sid_calls) == 2
    assert f"*{service_sid}:(R)" not in sid_calls[0]
    assert f"*{service_sid}:(R)" in sid_calls[1]
    assert takeown_call[-1] == "/A"
    assert "*S-1-5-21-1-2-3-500:(R)" in key_acl_call
    assert f"*{service_sid}:(M)" in key_acl_call
    assert source_call[2:] == [service_sid, "RX", "tree"]
    assert [call for call in calls if call[0] == "win32-acl"] == [
        ["win32-acl", str(installer.service_code_boundary_path), service_sid, "X", "direct"],
        [
            "win32-acl",
            str(installer.service_code_boundary_path / "backend"),
            service_sid,
            "X",
            "direct",
        ],
        source_call,
    ]


def test_injected_repair_installs_when_service_is_missing() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _Result(1 if command[:2] == ["sc.exe", "query"] else 0)

    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), runner=runner, is_windows=True)
    installer.repair()

    assert calls[0][:3] == ["sc.exe", "query", "PraxisSandboxBroker"]
    assert calls[1][:2] == ["sc.exe", "create"]


def test_injected_repair_reconfigures_service_and_accepts_already_running() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ["sc.exe", "start"]:
            return _Result(1056)
        if command[:2] == ["sc.exe", "stop"]:
            return _Result(1062)
        return _Result()

    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), runner=runner, is_windows=True)
    installer.repair()

    assert calls[1] == ["sc.exe", "stop", "PraxisSandboxBroker"]
    config = calls[2]
    assert config[:2] == ["sc.exe", "config"]
    assert config[config.index("obj=") + 1] == r"NT SERVICE\PraxisSandboxBroker"
    assert config[config.index("binPath=") + 1] == "python.exe -m broker"
    assert ["sc.exe", "sidtype", "PraxisSandboxBroker", "unrestricted"] in calls


def _install_fake_pywin32(monkeypatch: pytest.MonkeyPatch, shell_execute, *, exit_code: int = 0):
    shell_module = types.ModuleType("win32com.shell")
    shell_module.shell = types.SimpleNamespace(ShellExecuteEx=shell_execute)
    win32com = types.ModuleType("win32com")
    win32com.shell = shell_module
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.shell", shell_module)
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(SEE_MASK_NOCLOSEPROCESS=64, SW_HIDE=0))
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        types.SimpleNamespace(INFINITE=-1, WaitForSingleObject=lambda handle, timeout: 0),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        types.SimpleNamespace(GetExitCodeProcess=lambda handle: exit_code),
    )
    monkeypatch.setitem(sys.modules, "win32api", types.SimpleNamespace(CloseHandle=lambda handle: None))


def test_elevated_transaction_uses_current_source_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    source_root = tmp_path / "backend" / "src"

    def shell_execute(**kwargs):
        calls.append(kwargs)
        return {"hProcess": object()}

    _install_fake_pywin32(monkeypatch, shell_execute)
    installer = WindowsServiceInstaller(
        ("python.exe", "-m", "broker"),
        is_windows=True,
        service_code_path=source_root,
        service_code_boundary_path=tmp_path,
    )
    installer._run_elevated_transaction("install", None)

    assert len(calls) == 1
    assert calls[0]["lpVerb"] == "runas"
    assert calls[0]["lpFile"] == sys.executable
    assert "backend.sandbox.install_helper" in str(calls[0]["lpParameters"])
    assert calls[0]["lpDirectory"] == str(source_root)


def test_elevated_helper_bootstrap_executes_the_declared_source_root(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    sandbox_package = source_root / "sandbox"
    sandbox_package.mkdir(parents=True)
    (source_root / "__init__.py").write_text("", encoding="utf-8")
    (sandbox_package / "__init__.py").write_text("", encoding="utf-8")
    (sandbox_package / "install_helper.py").write_text("def main():\n    return 73\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, *_elevated_helper_argv("payload", source_root)],
        check=False,
        capture_output=True,
    )

    assert result.returncode == 73


def test_elevated_transaction_classifies_uac_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    class UacCancelledError(RuntimeError):
        winerror = 1223

    def shell_execute(**kwargs):
        raise UacCancelledError()

    _install_fake_pywin32(monkeypatch, shell_execute)
    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), is_windows=True)

    with pytest.raises(BrokerInstallationError) as raised:
        installer._run_elevated_transaction("install", None)

    assert raised.value.broker_code is BrokerInstallFailureCode.UAC_CANCELLED
    assert "UAC" in raised.value.safe_message


@pytest.mark.parametrize(
    ("exit_code", "expected_code"),
    [
        (4, BrokerInstallFailureCode.ACL_FAILED),
        (5, BrokerInstallFailureCode.SERVICE_START_FAILED),
        (7, BrokerInstallFailureCode.SERVICE_STOP_FAILED),
        (12, BrokerInstallFailureCode.BUSY),
        (13, BrokerInstallFailureCode.DEPENDENCY_MISSING),
    ],
)
def test_elevated_transaction_classifies_helper_phases(
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    expected_code: BrokerInstallFailureCode,
) -> None:
    _install_fake_pywin32(monkeypatch, lambda **kwargs: {"hProcess": object()}, exit_code=exit_code)
    installer = WindowsServiceInstaller(("python.exe", "-m", "broker"), is_windows=True)

    with pytest.raises(BrokerInstallationError) as raised:
        installer._run_elevated_transaction("repair", None)

    assert raised.value.broker_code is expected_code


def test_install_route_returns_safe_category_and_code() -> None:
    class Broker:
        def status(self):
            return {"installed": False, "healthy": False}

        def install(self):
            raise BrokerInstallationError(
                BrokerInstallFailureCode.ADMIN_REQUIRED,
                "需要管理员权限才能安装沙箱 Broker。",
            )

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                sandbox_maintenance=SandboxMaintenanceGate(),
                sandbox_manifest_path=Path("nonexistent-test-manifest.json"),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/install",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    response = install_broker(request)

    assert response.status_code == 503
    payload = json.loads(response.body)
    report = payload.pop("error_report")
    assert report["type"] == "BrokerInstallationError"
    assert report["traceback"]
    assert payload == {
        "detail": "需要管理员权限才能安装沙箱 Broker。",
        "code": "broker_admin_required",
    }


def test_repair_route_returns_safe_stop_category_and_code() -> None:
    message = "Broker Windows 服务未能停止，请稍后重试或重启 Windows。"

    class Broker:
        def status(self):
            return {"installed": True, "healthy": False}

        def repair(self):
            raise BrokerInstallationError(BrokerInstallFailureCode.SERVICE_STOP_FAILED, message)

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                sandbox_maintenance=SandboxMaintenanceGate(),
                sandbox_manifest_path=Path("nonexistent-test-manifest.json"),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    response = repair_broker(request)

    assert response.status_code == 503
    payload = json.loads(response.body)
    report = payload.pop("error_report")
    assert report["type"] == "BrokerInstallationError"
    assert report["traceback"]
    assert payload == {
        "detail": message,
        "code": "broker_service_stop_failed",
    }


@pytest.mark.parametrize("failure_code", list(BrokerInstallFailureCode))
def test_repair_route_preserves_every_failure_code_and_complete_detail(
    failure_code: BrokerInstallFailureCode,
) -> None:
    message = f"  raw repair detail for {failure_code.value}\nsecond line\n"

    class Broker:
        def status(self):
            return {"installed": True, "healthy": False}

        def repair(self):
            raise BrokerInstallationError(failure_code, message)

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                sandbox_maintenance=SandboxMaintenanceGate(),
                sandbox_manifest_path=Path("nonexistent-test-manifest.json"),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    response = repair_broker(request)

    assert response.status_code == 503
    payload = json.loads(response.body)
    report = payload.pop("error_report")
    assert report["type"] == "BrokerInstallationError"
    assert report["traceback"]
    assert payload == {"detail": message, "code": failure_code.value}


def test_repair_route_installs_when_broker_is_missing() -> None:
    calls: list[str] = []

    class Broker:
        def status(self):
            return {"installed": False, "healthy": False}

        def install(self):
            calls.append("install")
            return {"installed": True, "healthy": True}

        def repair(self):
            calls.append("repair")
            return {"installed": True, "healthy": True}

    app = types.SimpleNamespace(
        state=types.SimpleNamespace(
            web=types.SimpleNamespace(
                sandbox_broker=Broker(),
                sandbox_maintenance=SandboxMaintenanceGate(),
                sandbox_manifest_path=Path("nonexistent-test-manifest.json"),
            )
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": app,
        }
    )

    assert repair_broker(request) == {"installed": True, "healthy": True}
    assert calls == ["install"]


def test_maintenance_gate_rejects_overlap_in_both_directions() -> None:
    gate = SandboxMaintenanceGate()
    command = gate.acquire_command()
    with pytest.raises(SandboxMaintenanceBusy):
        gate.acquire_maintenance()
    command.close()

    maintenance = gate.acquire_maintenance()
    with pytest.raises(SandboxMaintenanceBusy):
        gate.acquire_command()
    maintenance.close()
    assert gate.active_commands == 0
    assert gate.maintenance_active is False


def test_repair_route_forces_healthy_broker_replacement(tmp_path: Path) -> None:
    calls: list[str] = []

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def reclaim_stale(self):
            calls.append("reclaim")
            return ()

        def repair(self):
            calls.append("repair")
            return {"installed": True, "healthy": True}

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "sandbox-leases.json").write_text("{}", encoding="utf-8")
    web = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=tmp_path / "resources.json",
        paths=types.SimpleNamespace(runtime_dir=runtime_dir),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    assert repair_broker(request) == {"installed": True, "healthy": True}
    assert calls == ["repair"]
    assert (runtime_dir / "sandbox-leases.json").read_text(encoding="utf-8") == "{}"


def test_repair_route_rejects_active_command_without_touching_broker(tmp_path: Path) -> None:
    gate = SandboxMaintenanceGate()
    command = gate.acquire_command()
    web = types.SimpleNamespace(
        sandbox_broker=types.SimpleNamespace(),
        sandbox_maintenance=gate,
        sandbox_manifest_path=tmp_path / "resources.json",
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )
    try:
        response = repair_broker(request)
    finally:
        command.close()

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "broker_jobs_active"


@pytest.mark.parametrize(
    ("manifest", "expected_status", "expected_code"),
    [
        ({"records": [{"job_id": "job-active"}]}, 409, "broker_jobs_active"),
        ({"unexpected": []}, 503, "broker_install_failed"),
    ],
)
def test_repair_route_rejects_unreclaimed_or_invalid_manifest(
    tmp_path: Path,
    manifest: dict[str, object],
    expected_status: int,
    expected_code: str,
) -> None:
    calls: list[str] = []

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def reclaim_stale(self):
            calls.append("reclaim")
            return ()

        def repair(self):
            calls.append("repair")
            return {"installed": True, "healthy": True}

    manifest_path = tmp_path / "resources.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    web = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=manifest_path,
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    response = repair_broker(request)

    assert response.status_code == expected_status
    assert json.loads(response.body)["code"] == expected_code
    assert calls == (["reclaim"] if expected_status == 409 else [])


def test_repair_route_reclaims_confirmed_stale_manifest_before_replacement(tmp_path: Path) -> None:
    calls: list[str] = []
    manifest_path = tmp_path / "resources.json"
    manifest_path.write_text(json.dumps({"records": [{"job_id": "stale"}]}), encoding="utf-8")

    class Broker:
        def status(self):
            return {"installed": True, "healthy": True}

        def reclaim_stale(self):
            calls.append("reclaim")
            manifest_path.write_text(json.dumps({"records": []}), encoding="utf-8")
            return ("stale",)

        def repair(self):
            calls.append("repair")
            return {"installed": True, "healthy": True}

    web = types.SimpleNamespace(
        sandbox_broker=Broker(),
        sandbox_maintenance=SandboxMaintenanceGate(),
        sandbox_manifest_path=manifest_path,
        paths=types.SimpleNamespace(runtime_dir=tmp_path),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sandbox/repair",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "app": types.SimpleNamespace(state=types.SimpleNamespace(web=web)),
        }
    )

    assert repair_broker(request) == {"installed": True, "healthy": True}
    assert calls == ["reclaim", "repair"]


@pytest.mark.parametrize("stop_returncode", [0, 1])
def test_repair_waits_for_stopped_after_any_stop_result(stop_returncode: int) -> None:
    calls: list[list[str]] = []
    states = iter([3, 1])

    _stop_service_for_repair(
        "PraxisSandboxBroker",
        runner=lambda command, **kwargs: calls.append(list(command)) or _Result(stop_returncode),
        state_reader=lambda service_name: next(states),
        clock=lambda: 0.0,
        sleeper=lambda seconds: None,
    )

    assert calls == [["sc.exe", "stop", "PraxisSandboxBroker"]]


def test_repair_stop_timeout_has_dedicated_exit_code() -> None:
    ticks = iter([0.0, 0.0, 5.0])

    with pytest.raises(_TransactionFailure) as raised:
        _stop_service_for_repair(
            "PraxisSandboxBroker",
            runner=lambda command, **kwargs: _Result(),
            state_reader=lambda service_name: 4,
            clock=lambda: next(ticks),
            sleeper=lambda seconds: None,
        )

    assert raised.value.exit_code == EXIT_SERVICE_STOP_FAILED


def test_repair_state_query_failure_aborts_before_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "backend.sandbox.install_helper.subprocess.run",
        lambda command, **kwargs: calls.append(list(command)) or _Result(),
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._query_service_state",
        lambda service_name: (_ for _ in ()).throw(OSError("unavailable")),
    )

    with pytest.raises(_TransactionFailure) as raised:
        run_transaction(
            {
                "operation": "repair",
                "service_name": "PraxisSandboxBroker",
                "service_command": ["python.exe", "-m", "backend.sandbox.service_main", "run"],
                "service_class": "sandbox_service_bootstrap.PraxisSandboxBrokerService",
                "backend_sid": None,
                "backend_sid_path": None,
                "program_data_path": None,
                "service_code_path": None,
                "service_code_boundary_path": None,
            }
        )

    assert raised.value.exit_code == EXIT_SERVICE_STOP_FAILED
    assert calls == [["sc.exe", "stop", "PraxisSandboxBroker"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows icacls test")
def test_numeric_sid_is_prefixed_for_icacls(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _icacls_sid("S-1-5-21-1-2-3-500") == "*S-1-5-21-1-2-3-500"
    with pytest.raises(ValueError):
        _icacls_sid("Administrator")

    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: "S-1-5-21-1-2-3-500")
    monkeypatch.setattr(
        "backend.sandbox.install_helper.subprocess.run",
        lambda command, **kwargs: calls.append(list(command)) or Result(),
    )
    _secure_program_data(
        Path("C:/ProgramData/Praxis/SandboxBroker"),
        Path("C:/ProgramData/Praxis/SandboxBroker/backend.sid"),
        "PraxisSandboxBroker",
    )

    assert "*S-1-5-21-1-2-3-500:(OI)(CI)(M)" in calls[0]
    assert f"*{_service_sid('PraxisSandboxBroker')}:(R)" in calls[1]


def test_service_sid_matches_windows_virtual_account() -> None:
    assert _service_sid("PraxisSandboxBroker") == ("S-1-5-80-2016461151-3834670916-2240187935-1380477427-2977801491")


def test_persist_sid_atomically_replaces_an_existing_file(tmp_path: Path) -> None:
    sid_path = tmp_path / "backend.sid"
    sid_path.write_text("stale", encoding="ascii")

    _persist_sid(sid_path, "S-1-5-21-1-2-3-500")

    assert sid_path.read_text(encoding="ascii") == "S-1-5-21-1-2-3-500"
    assert list(tmp_path.glob(".backend.sid.*")) == []


def test_invalid_sid_is_rejected_before_replacing_existing_file(tmp_path: Path) -> None:
    sid_path = tmp_path / "backend.sid"
    sid_path.write_text("existing", encoding="ascii")

    with pytest.raises(ValueError):
        _persist_sid(sid_path, "Administrator")

    assert sid_path.read_text(encoding="ascii") == "existing"


@pytest.mark.skipif(os.name != "nt", reason="Windows source ACL test")
def test_source_acl_is_confined_to_rx_on_source_tree() -> None:
    source = Path("C:/workspace/praxis/backend/src")
    boundary = Path("C:/workspace/praxis")

    grants = _source_acl_grants(source, boundary, "PraxisSandboxBroker")
    service_sid = _service_sid("PraxisSandboxBroker")

    assert [grant.path for grant in grants] == [boundary, boundary / "backend", source]
    assert [grant.rights for grant in grants] == ["X", "X", "RX"]
    assert [grant.inherit for grant in grants] == [False, False, True]
    assert grants[-1].sid == service_sid
    assert grants[-1].rights == "RX"
    assert grants[-1].inherit is True


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime ACL test")
def test_runtime_acl_requires_the_service_executable_and_grants_rx() -> None:
    runtime = Path("C:/workspace/praxis/.venv")
    base_runtime = Path("C:/python/cpython-3.12")
    executable = runtime / "Scripts" / "python.exe"

    grants = _runtime_acl_grants((runtime, base_runtime, runtime), executable, "PraxisSandboxBroker")

    assert [grant.path for grant in grants] == [runtime, base_runtime]
    assert all(grant.rights == "RX" and grant.inherit and grant.existing_children for grant in grants)
    with pytest.raises(ValueError):
        _runtime_acl_grants((base_runtime,), executable, "PraxisSandboxBroker")


def test_acl_tree_applies_rx_to_existing_children_and_future_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "runtime").resolve()
    package = root / "Lib" / "site-packages"
    package.mkdir(parents=True)
    executable = root / "pythonservice.exe"
    module = package / "servicemanager.pyd"
    executable.write_bytes(b"host")
    module.write_bytes(b"module")
    applied: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        access_policy,
        "_apply_acl_target",
        lambda path, _sid, _rights, *, inherit: applied.append((path, inherit)),
    )

    access_policy._apply_source_acl_grant(
        access_policy._SourceAclGrant(root, "S-1-5-80-1-2-3-4-5", "RX", True, existing_children=True)
    )

    assert set(applied) == {
        (root, True),
        (root / "Lib", True),
        (package, True),
        (executable, False),
        (module, False),
    }


def test_acl_tree_skips_reparse_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = (tmp_path / "runtime").resolve()
    skipped = root / "linked-runtime"
    skipped.mkdir(parents=True)
    (skipped / "outside.dll").write_bytes(b"outside")
    kept = root / "pythonservice.exe"
    kept.write_bytes(b"host")
    original = access_policy._is_reparse_point
    monkeypatch.setattr(
        access_policy,
        "_is_reparse_point",
        lambda path: path == skipped or original(path),
    )

    assert list(access_policy._iter_acl_tree(root)) == [(root, True), (kept, False)]


def test_acl_tree_rejects_reparse_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    monkeypatch.setattr(access_policy, "_is_reparse_point", lambda _path: True)

    with pytest.raises(ValueError, match="reparse point"):
        list(access_policy._iter_acl_tree(root))


def test_acl_target_is_idempotent_and_preserves_unrelated_aces(monkeypatch: pytest.MonkeyPatch) -> None:
    unrelated_sid = "S-1-5-21-unrelated"
    service_sid = "S-1-5-80-service"

    class Dacl:
        def __init__(self) -> None:
            self.aces = [((0, 0), 0x01, unrelated_sid)]

        def GetAceCount(self) -> int:
            return len(self.aces)

        def GetAce(self, index: int):
            return self.aces[index]

        def AddAccessAllowedAceEx(self, _revision: int, inheritance: int, access: int, sid: str) -> None:
            self.aces.append(((0, inheritance), access, sid))

    dacl = Dacl()
    descriptor = types.SimpleNamespace(GetSecurityDescriptorDacl=lambda: dacl)
    security = types.SimpleNamespace(
        ACCESS_ALLOWED_ACE_TYPE=0,
        ACL_REVISION_DS=4,
        CONTAINER_INHERIT_ACE=2,
        OBJECT_INHERIT_ACE=1,
        INHERITED_ACE=0x10,
        SE_FILE_OBJECT=1,
        DACL_SECURITY_INFORMATION=4,
        ACL=Dacl,
        ConvertStringSidToSid=lambda value: value,
        GetNamedSecurityInfo=lambda *_args: descriptor,
        SetNamedSecurityInfo=lambda *_args: None,
    )
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setitem(
        sys.modules,
        "ntsecuritycon",
        types.SimpleNamespace(FILE_GENERIC_READ=0x10, FILE_GENERIC_EXECUTE=0x20, FILE_TRAVERSE=0x40),
    )

    access_policy._apply_acl_target(Path("C:/runtime"), service_sid, "RX", inherit=True)
    access_policy._apply_acl_target(Path("C:/runtime"), service_sid, "RX", inherit=True)

    assert dacl.aces[0] == ((0, 0), 0x01, unrelated_sid)
    assert [ace for ace in dacl.aces if ace[2] == service_sid] == [((0, 3), 0x30, service_sid)]


def test_acl_target_reuses_sufficient_inherited_rx(monkeypatch: pytest.MonkeyPatch) -> None:
    service_sid = "S-1-5-80-service"

    class Dacl:
        def __init__(self) -> None:
            self.aces = [((0, 0x10), 0x30, service_sid)]

        def GetAceCount(self) -> int:
            return len(self.aces)

        def GetAce(self, index: int):
            return self.aces[index]

        def AddAccessAllowedAceEx(self, _revision: int, inheritance: int, access: int, sid: str) -> None:
            self.aces.append(((0, inheritance), access, sid))

    dacl = Dacl()
    security = types.SimpleNamespace(
        ACCESS_ALLOWED_ACE_TYPE=0,
        ACL_REVISION_DS=4,
        CONTAINER_INHERIT_ACE=2,
        OBJECT_INHERIT_ACE=1,
        INHERITED_ACE=0x10,
        SE_FILE_OBJECT=1,
        DACL_SECURITY_INFORMATION=4,
        ACL=Dacl,
        ConvertStringSidToSid=lambda value: value,
        GetNamedSecurityInfo=lambda *_args: types.SimpleNamespace(GetSecurityDescriptorDacl=lambda: dacl),
        SetNamedSecurityInfo=lambda *_args: None,
    )
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setitem(
        sys.modules,
        "ntsecuritycon",
        types.SimpleNamespace(FILE_GENERIC_READ=0x10, FILE_GENERIC_EXECUTE=0x20, FILE_TRAVERSE=0x40),
    )

    access_policy._apply_acl_target(Path("C:/runtime/module.pyd"), service_sid, "RX", inherit=False)

    assert dacl.aces == [((0, 0x10), 0x30, service_sid)]


@pytest.mark.skipif(os.name != "nt", reason="Windows Broker installation transaction test")
def test_helper_transaction_orders_sid_service_acl_source_and_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    program_data = tmp_path / "Praxis" / "SandboxBroker"
    sid_path = program_data / "backend.sid"
    source = tmp_path / "repo" / "backend" / "src"

    monkeypatch.setattr(
        "backend.sandbox.install_helper.subprocess.run",
        lambda command, **kwargs: calls.append(list(command)) or _Result(),
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._secure_source_code",
        lambda path, boundary, service_name: calls.append(["win32-acl-batch", str(path), str(boundary), service_name]),
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._wait_for_service_stopped",
        lambda service_name, **kwargs: True,
    )
    package = BrokerCredentialPackage(
        "generation-test",
        "PraxisSbxOffline",
        "S-1-5-21-1-2-3-1001",
        "offline-password",
        "PraxisSbxOnline",
        "S-1-5-21-1-2-3-1002",
        "online-password",
    )
    monkeypatch.setattr(
        "backend.sandbox.install_helper._provision_fixed_accounts",
        lambda *_args: (package, build_ready_marker(package, 17831)),
    )

    assert (
        run_transaction(
            {
                "operation": "repair",
                "service_name": "PraxisSandboxBroker",
                "service_command": ["python.exe", "-m", "backend.sandbox.service_main", "run"],
                "service_class": rf"{source}\sandbox_service_bootstrap.PraxisSandboxBrokerService",
                "backend_sid": "S-1-5-21-1-2-3-500",
                "backend_sid_path": str(sid_path),
                "program_data_path": str(program_data),
                "service_code_path": str(source),
                "service_code_boundary_path": str(tmp_path / "repo"),
            }
        )
        == 0
    )

    assert calls[0] == ["sc.exe", "stop", "PraxisSandboxBroker"]
    assert calls[1][:2] == ["icacls.exe", str(sid_path)]
    service_sid = f"*{_service_sid('PraxisSandboxBroker')}"
    assert f"{service_sid}:(R)" not in calls[1]
    assert calls[2][:2] == ["sc.exe", "config"]
    assert calls[3] == [
        "reg.exe",
        "add",
        r"HKLM\SYSTEM\CurrentControlSet\Services\PraxisSandboxBroker\PythonClass",
        "/ve",
        "/t",
        "REG_SZ",
        "/d",
        rf"{source}\sandbox_service_bootstrap.PraxisSandboxBrokerService",
        "/f",
    ]
    assert calls[4] == ["sc.exe", "sidtype", "PraxisSandboxBroker", "unrestricted"]
    assert calls[5][:2] == ["icacls.exe", str(program_data)]
    assert calls[6][:2] == ["icacls.exe", str(sid_path)]
    assert f"{service_sid}:(R)" in calls[6]
    assert calls[-4] == ["win32-acl-batch", str(source), str(tmp_path / "repo"), "PraxisSandboxBroker"]
    assert calls[-3] == ["takeown.exe", "/F", str(program_data / "ready.json"), "/A"]
    assert calls[-2][:2] == ["icacls.exe", str(program_data / "ready.json")]
    assert "*S-1-5-21-1-2-3-500:(R)" in calls[-2]
    assert calls[-1] == ["sc.exe", "start", "PraxisSandboxBroker"]
