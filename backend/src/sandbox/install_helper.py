"""Privileged, single-transaction Windows Broker installer.

This module is launched once through UAC by :class:`WindowsServiceInstaller`.
It intentionally contains no network or application state and returns only a
small exit code to the unprivileged parent process.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .installation.access_policy import (
    _apply_source_acl_grant,
    _directory_contains,
    _icacls_sid,
    _managed_file_acl_commands,
    _program_data_acl_commands,
    _runtime_acl_grants,
    _secure_source_code,
    _sensitive_file_acl_commands,
    _service_class_command,
    _sid_acl_command,
)
from .installation.accounts import (
    provision_fixed_accounts as _provision_fixed_accounts_impl,
)
from .installation.contracts import (
    EXIT_ACCOUNT_FAILED,
    EXIT_ACL_FAILED,
    EXIT_CREDENTIAL_FAILED,
    EXIT_DEPENDENCY_FAILED,
    EXIT_FILESYSTEM_FAILED,
    EXIT_INVALID,
    EXIT_NETWORK_FAILED,
    EXIT_OK,
    EXIT_RIGHTS_FAILED,
    EXIT_SERVICE_FAILED,
    EXIT_SERVICE_START_FAILED,
    EXIT_SERVICE_STOP_FAILED,
)
from .installation.contracts import (
    TransactionFailure as _TransactionFailure,
)
from .installation.contracts import (
    validate_payload as _validate_payload,
)
from .installation.lock import installation_lock

_SERVICE_STOPPED = 1
_SERVICE_STOP_TIMEOUT_SECONDS = 5.0
_SERVICE_STOP_POLL_SECONDS = 0.1


def _run(
    command: Sequence[str],
    *,
    failure_code: int = EXIT_SERVICE_FAILED,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except OSError as exc:
        raise _TransactionFailure(EXIT_FILESYSTEM_FAILED, "Broker service command could not start") from exc
    if result.returncode not in accepted_returncodes:
        raise _TransactionFailure(
            failure_code,
            f"Command exited with code {result.returncode}: "
            + (getattr(result, "stderr", b"") or b"").decode(errors="replace"),
        )


def _query_service_state(service_name: str) -> int:
    """Read the numeric SCM state without parsing localized command output."""

    manager = None
    service = None
    win32service = None
    try:
        import win32service as win32service_module  # type: ignore[import-not-found]

        win32service = win32service_module
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        service = win32service.OpenService(manager, service_name, win32service.SERVICE_QUERY_STATUS)
        status = win32service.QueryServiceStatusEx(service)
        return int(status["CurrentState"])
    except Exception:
        raise
    finally:
        if win32service is not None and service is not None:
            try:
                win32service.CloseServiceHandle(service)
            except Exception:
                pass
        if win32service is not None and manager is not None:
            try:
                win32service.CloseServiceHandle(manager)
            except Exception:
                pass


def _wait_for_service_stopped(
    service_name: str,
    *,
    state_reader: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> bool:
    reader = state_reader or _query_service_state
    deadline = clock() + _SERVICE_STOP_TIMEOUT_SECONDS
    while True:
        try:
            state = int(reader(service_name))
        except Exception:
            return False
        if state == _SERVICE_STOPPED:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleeper(min(_SERVICE_STOP_POLL_SECONDS, remaining))


def _stop_service_for_repair(
    service_name: str,
    *,
    runner: Callable[..., Any] | None = None,
    state_reader: Callable[[str], int] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], Any] = time.sleep,
) -> None:
    command_runner = runner or subprocess.run
    try:
        command_runner(["sc.exe", "stop", service_name], check=False, capture_output=True)
    except OSError:
        # SCM state is authoritative: a command failure may race with the
        # service reaching STOPPED, so always perform the bounded state check.
        pass
    if _wait_for_service_stopped(
        service_name,
        state_reader=state_reader,
        clock=clock,
        sleeper=sleeper,
    ):
        return
    raise _TransactionFailure(EXIT_SERVICE_STOP_FAILED, "Broker service did not stop")


def _service_exists(service_name: str) -> bool:
    try:
        result = subprocess.run(["sc.exe", "query", service_name], check=False, capture_output=True)
    except OSError as exc:
        raise OSError("Broker service state is unavailable") from exc
    if int(result.returncode) == 1060:
        return False
    if int(result.returncode) != 0:
        raise _TransactionFailure(EXIT_SERVICE_FAILED, "Broker service state is unavailable")
    return True


def _persist_sid(path: Path | None, value: str | None) -> None:
    if path is None or value is None:
        return
    _icacls_sid(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace the file instead of writing in place.  Older installations
    # may have an empty/protected DACL on backend.sid, which can deny an
    # administrator an in-place write.  The parent directory is already
    # controlled by the elevated transaction and permits replacement.
    fd, temporary = tempfile.mkstemp(prefix=".backend.sid.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _secure_program_data(path: Path | None, sid_path: Path | None, service_name: str) -> None:
    if path is None or sid_path is None:
        return
    backend_sid = sid_path.read_text(encoding="ascii").strip()
    for command in _program_data_acl_commands(path, sid_path, backend_sid, service_name):
        _run(command, failure_code=EXIT_ACL_FAILED)
    for name in (
        "installation.id",
        "installation.key.dpapi",
        "ready.json",
        "control-plane.jsonl",
        "resources.json",
    ):
        managed_path = path / name
        if _directory_contains(path, name):
            for command in _managed_file_acl_commands(managed_path, backend_sid, service_name):
                _run(command, failure_code=EXIT_ACL_FAILED)
    credential_path = path / "accounts.dpapi"
    if _directory_contains(path, credential_path.name):
        for command in _sensitive_file_acl_commands(credential_path, service_name):
            _run(command, failure_code=EXIT_ACL_FAILED)


def _provision_fixed_accounts(data_path: Path, service_name: str, proxy_port: int):
    """Stable facade for tests and the elevated transaction orchestrator."""

    return _provision_fixed_accounts_impl(
        data_path,
        service_name,
        proxy_port,
        configure_network=_configure_static_network,
    )


def _configure_static_network(offline_sid: str, online_sid: str, proxy_port: int) -> None:
    from .native_windows.wfp import configure_static_wfp

    configure_static_wfp(offline_sid, online_sid, proxy_port)


def _initialize_installation_key(data_path: Path) -> None:
    try:
        from .broker_service.credentials import DpapiKeyStore

        DpapiKeyStore(data_path / "installation.key.dpapi").ensure()
    except Exception as exc:
        raise _TransactionFailure(EXIT_CREDENTIAL_FAILED, "Broker installation key could not be created") from exc


def run_transaction(payload: Mapping[str, Any]) -> int:
    """Serialize all privileged writes, including service-host preparation."""

    _validate_payload(payload)
    with installation_lock(str(payload["service_name"])):
        return _run_transaction(payload)


def _prepare_service_host(service_command: tuple[str, ...], service_class: str | None) -> tuple[str, ...]:
    if service_class is None:
        return service_command
    from .broker_service.installer import WindowsServiceInstaller
    from .errors import BrokerInstallationError

    installer = WindowsServiceInstaller(service_command, service_class=service_class)
    try:
        installer._prepare_service_host()
    except BrokerInstallationError as exc:
        exc.exit_code = EXIT_DEPENDENCY_FAILED
        raise
    return installer.service_command


def _run_transaction(payload: Mapping[str, Any]) -> int:

    (
        operation,
        service_name,
        service_command,
        service_class,
        backend_sid,
        sid_path,
        data_path,
        code_path,
        code_boundary_path,
        runtime_paths,
        proxy_port,
    ) = _validate_payload(payload)
    if operation == "start":
        from .control.service_state import start_service

        start_service(service_name)
        return EXIT_OK
    ready_path = data_path / "ready.json" if data_path is not None else None
    installed = _service_exists(service_name)
    if installed:
        _stop_service_for_repair(service_name)
    service_command = _prepare_service_host(service_command, service_class)
    _persist_sid(sid_path, backend_sid)
    if data_path is not None:
        _initialize_installation_key(data_path)
    if sid_path is not None and backend_sid is not None:
        # Restore a usable descriptor before touching SCM.  The service ACE is
        # added after the service exists and its virtual account is resolvable.
        _run(_sid_acl_command(sid_path, backend_sid, None), failure_code=EXIT_ACL_FAILED)
    command = subprocess.list2cmdline(list(service_command))
    if not installed:
        _run(
            [
                "sc.exe",
                "create",
                service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                f"NT SERVICE\\{service_name}",
                "binPath=",
                command,
            ]
        )
        if service_class is not None:
            _run(_service_class_command(service_name, service_class))
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    else:
        _run(
            [
                "sc.exe",
                "config",
                service_name,
                "type=",
                "own",
                "start=",
                "demand",
                "obj=",
                f"NT SERVICE\\{service_name}",
                "binPath=",
                command,
            ]
        )
        if service_class is not None:
            _run(_service_class_command(service_name, service_class))
        _run(["sc.exe", "sidtype", service_name, "unrestricted"])
    ready_marker = None
    if data_path is not None:
        _, ready_marker = _provision_fixed_accounts(data_path, service_name, proxy_port)
    _secure_program_data(data_path, sid_path, service_name)
    _secure_source_code(code_path, code_boundary_path, service_name)
    for grant in _runtime_acl_grants(runtime_paths, Path(service_command[0]), service_name):
        _apply_source_acl_grant(grant)
    if ready_path is not None and ready_marker is not None:
        from .broker_service.readiness import write_ready_marker

        write_ready_marker(ready_path, ready_marker)
        # os.replace preserves the temporary file's descriptor.  Apply the
        # explicit non-sensitive marker ACL after the atomic replacement so a
        # stale or token-default DACL cannot lock the normal backend out.
        if backend_sid is None:
            raise _TransactionFailure(EXIT_ACL_FAILED, "Broker backend SID is unavailable")
        for command in _managed_file_acl_commands(ready_path, backend_sid, service_name):
            _run(command, failure_code=EXIT_ACL_FAILED)
    _run(
        ["sc.exe", "start", service_name],
        failure_code=EXIT_SERVICE_START_FAILED,
        accepted_returncodes=frozenset({0, 1056}),
    )
    return EXIT_OK


def _decode_payload(value: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Broker installation payload must be an object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return EXIT_INVALID
    payload = None
    try:
        payload = _decode_payload(args[0])
        return run_transaction(payload)
    except Exception as exc:
        if payload is not None and payload.get("result_id") and payload.get("program_data_path"):
            try:
                from .installation.results import write_result

                _validate_payload(payload)
                write_result(Path(payload["program_data_path"]), payload["result_id"], exc, payload.get("backend_sid"))
            except Exception:
                pass  # Missing diagnostics must not replace the original transaction failure.
        if isinstance(getattr(exc, "exit_code", None), int):
            return exc.exit_code
        return EXIT_FILESYSTEM_FAILED if isinstance(exc, OSError) else EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_ACCOUNT_FAILED",
    "EXIT_ACL_FAILED",
    "EXIT_CREDENTIAL_FAILED",
    "EXIT_FILESYSTEM_FAILED",
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_NETWORK_FAILED",
    "EXIT_RIGHTS_FAILED",
    "EXIT_SERVICE_FAILED",
    "EXIT_SERVICE_START_FAILED",
    "EXIT_SERVICE_STOP_FAILED",
    "main",
    "run_transaction",
    "_runtime_acl_grants",
]
