"""Cross-process exclusion for the elevated Broker transaction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from .contracts import EXIT_BUSY, TransactionFailure


@contextmanager
def installation_lock(service_name: str) -> Iterator[None]:
    import pywintypes
    import win32api
    import win32event
    import win32security

    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:P(A;;GA;;;SY)(A;;GA;;;BA)", win32security.SDDL_REVISION_1
    )
    handle = win32event.CreateMutex(attributes, False, rf"Global\{service_name}.Installation")
    acquired = False
    try:
        result = win32event.WaitForSingleObject(handle, 0)
        acquired = result in {win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED}
        if not acquired:
            raise TransactionFailure(EXIT_BUSY, "Broker maintenance is already in progress")
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(handle)
        win32api.CloseHandle(handle)
