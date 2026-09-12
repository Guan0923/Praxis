"""Bound status probes without changing blocking command stream I/O."""

import time

from .pipe_connection import connect_pipe


def status_request(pipe_name: str, payload: bytes, timeout: float = 1.0) -> bytes:
    import pywintypes
    import win32event
    import win32file

    deadline = time.monotonic() + timeout
    pipe = connect_pipe(pipe_name, deadline=deadline, overlapped=True)
    try:

        def transfer(data: bytes | int, reading: bool) -> bytes:
            overlapped = pywintypes.OVERLAPPED()
            event = win32event.CreateEvent(None, True, False, None)
            overlapped.hEvent = event
            try:
                if reading:
                    _code, buffer = win32file.ReadFile(pipe, data, overlapped)
                else:
                    win32file.WriteFile(pipe, data, overlapped)
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if win32event.WaitForSingleObject(event, remaining) != win32event.WAIT_OBJECT_0:
                    failure = TimeoutError("Sandbox Broker status request timed out")
                    try:
                        win32file.CancelIoEx(pipe, overlapped)
                        win32event.WaitForSingleObject(event, win32event.INFINITE)
                    except Exception as cleanup:
                        failure.add_note(f"Cancelling status I/O failed: {cleanup}")
                    raise failure
                size = win32file.GetOverlappedResult(pipe, overlapped, False)
                return bytes(buffer[:size]) if reading else b""
            finally:
                event.Close()

        transfer(payload, False)
        return transfer(1024 * 1024, True)
    finally:
        pipe.Close()
