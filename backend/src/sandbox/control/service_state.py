"""Read and start an existing Windows service without changing its configuration."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

STATES = {
    1: "stopped",
    2: "start_pending",
    3: "stop_pending",
    4: "running",
    5: "continue_pending",
    6: "pause_pending",
    7: "paused",
}


@contextmanager
def _service(name: str, access: int) -> Iterator[Any]:
    import win32service

    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        handle = win32service.OpenService(manager, name, access)
        try:
            yield handle
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(manager)


def query_service(name: str) -> dict[str, str | int]:
    import win32service

    try:
        with _service(name, win32service.SERVICE_QUERY_STATUS) as handle:
            status = win32service.QueryServiceStatusEx(handle)
    except Exception as exc:
        if getattr(exc, "winerror", None) == 1060:
            return {"state": "missing", "exit_code": 0, "service_exit_code": 0}
        raise
    return {
        "state": STATES.get(status["CurrentState"], "unknown"),
        "exit_code": status["Win32ExitCode"],
        "service_exit_code": status["ServiceSpecificExitCode"],
    }


def start_service(name: str) -> None:
    import win32service

    with _service(name, win32service.SERVICE_START) as handle:
        try:
            win32service.StartService(handle, None)
        except Exception as exc:
            if getattr(exc, "winerror", None) != 1056:
                raise
