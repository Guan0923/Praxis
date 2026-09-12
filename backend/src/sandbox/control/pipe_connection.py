"""Connect to a Broker pipe without replaying an application request."""

import time
from typing import Any


def connect_pipe(pipe_name: str, *, deadline: float, overlapped: bool = False) -> Any:
    import pywintypes
    import win32con
    import win32file
    import win32pipe
    import winerror

    while True:
        try:
            return win32file.CreateFile(
                pipe_name,
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_OVERLAPPED if overlapped else 0,
                None,
            )
        except pywintypes.error as exc:
            if exc.winerror != winerror.ERROR_PIPE_BUSY:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Sandbox Broker pipe connection timed out") from exc
            try:
                win32pipe.WaitNamedPipe(pipe_name, max(1, int(remaining * 1000)))
            except pywintypes.error as wait_error:
                if wait_error.winerror == winerror.ERROR_SEM_TIMEOUT:
                    raise TimeoutError("Sandbox Broker pipe connection timed out") from wait_error
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("Sandbox Broker pipe connection timed out") from exc
